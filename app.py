import os
import uuid
import subprocess
import threading
import time
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, Response
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

UPLOAD_FOLDER = '/tmp/vc_uploads'
PROCESSED_FOLDER = '/tmp/vc_processed'
ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv'}
jobs = {}

def allowed_file(f):
    return '.' in f and f.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_resolution(path):
    try:
        r = subprocess.run([
            'ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height',
            '-of', 'csv=p=0', path
        ], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            parts = r.stdout.strip().split('\n')[0].split(',')
            if len(parts) == 2:
                return int(parts[0].strip()), int(parts[1].strip())
    except:
        pass
    return 1080, 1080

def build_scale(w, h):
    if w >= 1080 and h >= 1080:
        return '', w, h
    if w <= h:
        ow = 1080
        oh = (int(h * 1080 / w) // 2) * 2
        return 'scale=1080:-2:flags=lanczos,', ow, oh
    oh = 1080
    ow = (int(w * 1080 / h) // 2) * 2
    return 'scale=-2:1080:flags=lanczos,', ow, oh

def ffmpeg_run(cmd, job, label):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            # Filter meaningful error lines
            lines = [l for l in r.stderr.split('\n')
                     if l.strip() and not l.startswith('[') and 'frame=' not in l]
            err = lines[-1] if lines else 'Erro desconhecido'
            job['log'].append(f'[ERRO] {label}: {err}')
            job['status'] = 'error'
            job['error'] = err
            return False
        return True
    except subprocess.TimeoutExpired:
        job['status'] = 'error'
        job['error'] = 'Timeout — video muito longo'
        job['log'].append('[ERRO] Timeout no processamento')
        return False
    except Exception as e:
        job['status'] = 'error'
        job['error'] = str(e)
        job['log'].append(f'[ERRO] {e}')
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
        bc = now.strftime('%Y-%m-%dT%H:%M:')
        ct1 = f"{bc}{(now.second+1)%60:02d}.{ms}Z"
        ct2 = f"{bc}{(now.second+3)%60:02d}.{ms}Z"
        ct3 = f"{bc}{(now.second+7)%60:02d}.{ms}Z"

        out1 = os.path.join(output_dir, f'{basename}_v1.mp4')
        out2 = os.path.join(output_dir, f'{basename}_v2.mp4')
        out3 = os.path.join(output_dir, f'{basename}_v3.mp4')

        # Pre-compute zoom dimensions for V3
        zoom_w = (int(ow * 1.015) // 2) * 2
        zoom_h = (int(oh * 1.015) // 2) * 2

        vf1 = f'{sf}format=yuv420p'
        vf2 = f'{sf}crop={ow-8}:{oh-8}:4:4,scale={ow}:{oh}:flags=lanczos,eq=brightness=0.02:saturation=1.03:contrast=1.02,format=yuv420p'
        vf3 = f'{sf}crop={ow-16}:{oh-16}:8:8,scale={zoom_w}:{zoom_h}:flags=lanczos,crop={ow}:{oh},eq=brightness=0.03:saturation=1.05:gamma=1.02,format=yuv420p'

        steps = [
            {
                'label': 'Leve', 'out': out1,
                'cmd': [
                    'ffmpeg', '-y', '-i', input_path,
                    '-map_metadata', '-1', '-map_chapters', '-1',
                    '-vf', vf1,
                    '-c:v', 'libx264', '-crf', '20', '-preset', 'faster',
                    '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k',
                    '-movflags', '+faststart',
                    '-metadata', f'creation_time={ct1}',
                    out1
                ]
            },
            {
                'label': 'Medio', 'out': out2,
                'cmd': [
                    'ffmpeg', '-y', '-ss', '0.05', '-i', input_path,
                    '-map_metadata', '-1', '-map_chapters', '-1',
                    '-vf', vf2,
                    '-c:v', 'libx264', '-crf', '22', '-preset', 'faster', '-b:v', '5M',
                    '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k',
                    '-af', 'volume=0.98', '-movflags', '+faststart',
                    '-metadata', f'creation_time={ct2}',
                    out2
                ]
            },
            {
                'label': 'Forte', 'out': out3,
                'cmd': [
                    'ffmpeg', '-y', '-ss', '0.1', '-i', input_path,
                    '-map_metadata', '-1', '-map_chapters', '-1',
                    '-vf', vf3,
                    '-c:v', 'libx264', '-crf', '23', '-preset', 'fast', '-b:v', '4500k',
                    '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k',
                    '-af', 'atempo=1.005,volume=0.97', '-movflags', '+faststart',
                    '-metadata', f'creation_time={ct3}',
                    out3
                ]
            },
        ]

        outputs = []
        for i, step in enumerate(steps):
            job['log'].append(f'[{i+1}/3] Processando {step["label"]}...')
            if not ffmpeg_run(step['cmd'], job, step['label']):
                return
            size = os.path.getsize(step['out'])
            job['progress'] = int((i+1) / 3 * 100)
            job['log'].append(f'[OK] {step["label"]}: {os.path.basename(step["out"])} ({size//1024}KB)')
            outputs.append({
                'name': f'v{i+1}',
                'label': step['label'],
                'filename': os.path.basename(step['out']),
                'size': size,
                'resolution': f'{ow}x{oh}',
            })

        job['status'] = 'done'
        job['outputs'] = outputs
        job['resolution_in'] = f'{w}x{h}'
        job['resolution_out'] = f'{ow}x{oh}'
        job['log'].append('Concluido!')

    except Exception as e:
        job['status'] = 'error'
        job['error'] = str(e)
        job['log'].append(f'[ERRO] {e}')


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
    safe = secure_filename(file.filename) or f'video_{job_id}.mp4'
    basename = os.path.splitext(safe)[0]

    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    input_path = os.path.join(UPLOAD_FOLDER, f'{job_id}_{safe}')
    file.save(input_path)

    output_dir = os.path.join(PROCESSED_FOLDER, job_id)
    os.makedirs(output_dir, exist_ok=True)

    jobs[job_id] = {
        'id': job_id,
        'filename': safe,
        'status': 'queued',
        'progress': 0,
        'log': [],
        'outputs': [],
        'created_at': datetime.now().strftime('%d/%m/%Y %H:%M'),
    }

    t = threading.Thread(target=process_video,
                         args=(job_id, input_path, basename, output_dir),
                         daemon=True)
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
        return jsonify({'error': 'Nao encontrado'}), 404
    path = os.path.join(PROCESSED_FOLDER, job_id, filename)
    if not os.path.exists(path):
        return jsonify({'error': 'Arquivo nao encontrado'}), 404
    return send_file(path, as_attachment=True)

if __name__ == '__main__':
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(PROCESSED_FOLDER, exist_ok=True)
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
