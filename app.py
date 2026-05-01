import os
import uuid
import subprocess
import threading
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

UPLOAD_FOLDER = '/tmp/vc_uploads'
PROCESSED_FOLDER = '/tmp/vc_processed'
ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv'}
jobs = {}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_resolution(filepath):
    try:
        result = subprocess.run([
            'ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height',
            '-of', 'csv=p=0', filepath
        ], capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            line = result.stdout.strip().split('\n')[0]
            parts = line.split(',')
            if len(parts) == 2:
                return int(parts[0].strip()), int(parts[1].strip())
    except Exception as e:
        pass
    return 1080, 1080

def build_scale(w, h):
    if w >= 1080 and h >= 1080:
        return '', w, h
    if w <= h:
        ow = 1080
        oh = (int(h * 1080 / w) // 2) * 2
        return 'scale=1080:-2:flags=lanczos,', ow, oh
    else:
        oh = 1080
        ow = (int(w * 1080 / h) // 2) * 2
        return 'scale=-2:1080:flags=lanczos,', ow, oh

def run_ffmpeg(cmd, job, label):
    """Run ffmpeg command, return True on success"""
    job['log'].append(f'Rodando {label}...')
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            # Extract last meaningful error line
            err_lines = [l for l in result.stderr.split('\n') if l.strip() and not l.startswith('[')]
            err = err_lines[-1] if err_lines else result.stderr[-200:]
            job['log'].append(f'[ERRO] {label}: {err}')
            job['status'] = 'error'
            job['error'] = err
            return False
        return True
    except subprocess.TimeoutExpired:
        job['log'].append(f'[ERRO] {label}: timeout')
        job['status'] = 'error'
        job['error'] = 'Timeout no processamento'
        return False
    except Exception as e:
        job['log'].append(f'[ERRO] {label}: {str(e)}')
        job['status'] = 'error'
        job['error'] = str(e)
        return False

def process_video(job_id, input_path, basename, output_dir):
    job = jobs[job_id]
    job['status'] = 'processing'
    job['progress'] = 0
    job['log'] = []

    try:
        w, h = get_resolution(input_path)
        job['log'].append(f'Resolucao original: {w}x{h}')

        sf, ow, oh = build_scale(w, h)
        if sf:
            job['log'].append(f'[AVISO] Upscale para {ow}x{oh} (minimo Meta Ads)')
        else:
            job['log'].append('Resolucao OK para Meta Ads')

        now = datetime.utcnow()
        ms = now.strftime('%f')[:3]
        base_ct = now.strftime('%Y-%m-%dT%H:%M:')

        # Build output paths explicitly
        out1 = os.path.join(output_dir, f'{basename}_v1.mp4')
        out2 = os.path.join(output_dir, f'{basename}_v2.mp4')
        out3 = os.path.join(output_dir, f'{basename}_v3.mp4')

        ct1 = f"{base_ct}{(now.second+1)%60:02d}.{ms}Z"
        ct2 = f"{base_ct}{(now.second+3)%60:02d}.{ms}Z"
        ct3 = f"{base_ct}{(now.second+7)%60:02d}.{ms}Z"

        vf1 = f'{sf}format=yuv420p'
        vf2 = f'{sf}crop={ow-8}:{oh-8}:4:4,scale={ow}:{oh}:flags=lanczos,eq=brightness=0.02:saturation=1.03:contrast=1.02,format=yuv420p'
        vf3 = f'{sf}crop={ow-16}:{oh-16}:8:8,scale=iw*1.015:ih*1.015:flags=lanczos,crop={ow}:{oh},eq=brightness=0.03:saturation=1.05:gamma=1.02,format=yuv420p'

        # V1 - Leve
        job['log'].append('[1/3] Processando Leve...')
        cmd1 = [
            'ffmpeg', '-y',
            '-i', input_path,
            '-map_metadata', '-1', '-map_chapters', '-1',
            '-vf', vf1,
            '-c:v', 'libx264', '-crf', '20', '-preset', 'medium',
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '128k',
            '-movflags', '+faststart',
            '-metadata', f'creation_time={ct1}',
            out1
        ]
        if not run_ffmpeg(cmd1, job, 'V1'): return
        job['progress'] = 33
        job['log'].append(f'[OK] Leve: {os.path.basename(out1)} ({os.path.getsize(out1)//1024}KB)')

        # V2 - Medio
        job['log'].append('[2/3] Processando Medio...')
        cmd2 = [
            'ffmpeg', '-y',
            '-ss', '0.05', '-i', input_path,
            '-map_metadata', '-1', '-map_chapters', '-1',
            '-vf', vf2,
            '-c:v', 'libx264', '-crf', '22', '-preset', 'medium', '-b:v', '5M',
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '128k',
            '-af', 'volume=0.98',
            '-movflags', '+faststart',
            '-metadata', f'creation_time={ct2}',
            out2
        ]
        if not run_ffmpeg(cmd2, job, 'V2'): return
        job['progress'] = 66
        job['log'].append(f'[OK] Medio: {os.path.basename(out2)} ({os.path.getsize(out2)//1024}KB)')

        # V3 - Forte
        job['log'].append('[3/3] Processando Forte...')
        cmd3 = [
            'ffmpeg', '-y',
            '-ss', '0.1', '-i', input_path,
            '-map_metadata', '-1', '-map_chapters', '-1',
            '-vf', vf3,
            '-c:v', 'libx264', '-crf', '23', '-preset', 'slow', '-b:v', '4500k',
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '128k',
            '-af', 'atempo=1.005,volume=0.97',
            '-movflags', '+faststart',
            '-metadata', f'creation_time={ct3}',
            out3
        ]
        if not run_ffmpeg(cmd3, job, 'V3'): return
        job['progress'] = 100
        job['log'].append(f'[OK] Forte: {os.path.basename(out3)} ({os.path.getsize(out3)//1024}KB)')

        job['status'] = 'done'
        job['resolution_in'] = f'{w}x{h}'
        job['resolution_out'] = f'{ow}x{oh}'
        job['outputs'] = [
            {'name': 'v1', 'label': 'Leve', 'filename': os.path.basename(out1), 'size': os.path.getsize(out1), 'resolution': f'{ow}x{oh}'},
            {'name': 'v2', 'label': 'Medio', 'filename': os.path.basename(out2), 'size': os.path.getsize(out2), 'resolution': f'{ow}x{oh}'},
            {'name': 'v3', 'label': 'Forte', 'filename': os.path.basename(out3), 'size': os.path.getsize(out3), 'resolution': f'{ow}x{oh}'},
        ]
        job['log'].append('Concluido!')

    except Exception as e:
        job['status'] = 'error'
        job['error'] = str(e)
        job['log'].append(f'[ERRO] Excecao: {str(e)}')


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return jsonify({'error': 'Nenhum arquivo'}), 400
    file = request.files['file']
    if not file or not allowed_file(file.filename):
        return jsonify({'error': 'Formato invalido. Use mp4, mov, avi ou mkv'}), 400

    job_id = str(uuid.uuid4())[:8]
    safe_name = secure_filename(file.filename)
    if not safe_name:
        safe_name = f'video_{job_id}.mp4'
    basename = os.path.splitext(safe_name)[0]

    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    input_path = os.path.join(UPLOAD_FOLDER, f'{job_id}_{safe_name}')
    file.save(input_path)

    output_dir = os.path.join(PROCESSED_FOLDER, job_id)
    os.makedirs(output_dir, exist_ok=True)

    jobs[job_id] = {
        'id': job_id,
        'filename': safe_name,
        'status': 'queued',
        'progress': 0,
        'log': [],
        'outputs': [],
        'created_at': datetime.now().strftime('%d/%m/%Y %H:%M'),
    }

    t = threading.Thread(target=process_video, args=(job_id, input_path, basename, output_dir))
    t.daemon = True
    t.start()

    return jsonify({'job_id': job_id})

@app.route('/status/<job_id>')
def status(job_id):
    if job_id not in jobs:
        return jsonify({'error': 'Job nao encontrado'}), 404
    return jsonify(jobs[job_id])

@app.route('/download/<job_id>/<filename>')
def download(job_id, filename):
    if job_id not in jobs:
        return jsonify({'error': 'Job nao encontrado'}), 404
    path = os.path.join(PROCESSED_FOLDER, job_id, filename)
    if not os.path.exists(path):
        return jsonify({'error': 'Arquivo nao encontrado'}), 404
    return send_file(path, as_attachment=True)

if __name__ == '__main__':
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(PROCESSED_FOLDER, exist_ok=True)
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
