import os
import uuid
import subprocess
import threading
import time
import json
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, url_for
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['PROCESSED_FOLDER'] = 'processed'

ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv'}

# Job store in memory
jobs = {}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_resolution(filepath):
    result = subprocess.run([
        'ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height',
        '-of', 'csv=p=0', filepath
    ], capture_output=True, text=True)
    if result.returncode == 0:
        parts = result.stdout.strip().split(',')
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    return 1080, 1080

def build_scale_filter(w, h):
    if w < 1080 or h < 1080:
        if w <= h:
            out_w = 1080
            out_h = int((h * 1080) / w)
            out_h = (out_h // 2) * 2
            return f"scale=1080:-2:flags=lanczos,", out_w, out_h
        else:
            out_h = 1080
            out_w = int((w * 1080) / h)
            out_w = (out_w // 2) * 2
            return f"scale=-2:1080:flags=lanczos,", out_w, out_h
    return "", w, h

def process_video(job_id, input_path, basename, output_dir):
    job = jobs[job_id]
    job['status'] = 'processing'
    job['progress'] = 0
    job['log'] = []

    def log(msg):
        job['log'].append(msg)

    try:
        w, h = get_resolution(input_path)
        log(f"Resolucao original: {w}x{h}")
        scale_filter, out_w, out_h = build_scale_filter(w, h)

        if scale_filter:
            log(f"Upscale necessario -> {out_w}x{out_h}")
        else:
            log("Resolucao ok para Meta Ads")

        now = datetime.utcnow()
        micro = now.strftime("%f")[:3]
        ct_base = now.strftime("%Y-%m-%dT%H:%M:")

        enc_tags = [
            ("Lavf59.16.100", "isom", "0"),
            ("HandBrake 1.6.1 2023060300", "mp42", "512"),
            ("DaVinci Resolve 18.5.0.42", "M4V ", "1"),
        ]

        versions = [
            {
                "name": "v1", "label": "Leve",
                "args": ["-crf", "20", "-preset", "medium"],
                "vf": f"{scale_filter}format=yuv420p",
                "af": None,
                "ss": None,
            },
            {
                "name": "v2", "label": "Medio",
                "args": ["-crf", "22", "-preset", "medium", "-b:v", "5M"],
                "vf": f"{scale_filter}crop={out_w-8}:{out_h-8}:4:4,scale={out_w}:{out_h}:flags=lanczos,eq=brightness=0.02:saturation=1.03:contrast=1.02,format=yuv420p",
                "af": "volume=0.98",
                "ss": "0.05",
            },
            {
                "name": "v3", "label": "Forte",
                "args": ["-crf", "23", "-preset", "slow", "-b:v", "4500k"],
                "vf": f"{scale_filter}crop={out_w-16}:{out_h-16}:8:8,scale=iw*1.015:ih*1.015:flags=lanczos,crop={out_w}:{out_h},eq=brightness=0.03:saturation=1.05:gamma=1.02,format=yuv420p",
                "af": "atempo=1.005,volume=0.97",
                "ss": "0.1",
            },
        ]

        outputs = []

        for i, ver in enumerate(versions):
            enc, brand, minor = enc_tags[i]
            sec_offset = (now.second + (i+1)*2) % 60
            ct = f"{ct_base}{sec_offset:02d}.{micro}Z"

            out_filename = f"{basename}_{ver['name']}.mp4"
            out_path = os.path.join(output_dir, out_filename)

            cmd = ["ffmpeg", "-y"]
            if ver['ss']:
                cmd += ["-ss", ver['ss']]
            cmd += ["-i", input_path]
            cmd += ["-map_metadata", "-1", "-map_chapters", "-1"]
            cmd += ["-vf", ver['vf']]
            cmd += ["-c:v", "libx264"] + ver['args']
            cmd += ["-pix_fmt", "yuv420p"]
            cmd += ["-c:a", "aac", "-b:a", "128k"]
            if ver['af']:
                cmd += ["-af", ver['af']]
            cmd += ["-movflags", "+faststart"]
            cmd += ["-metadata", f"creation_time={ct}"]
            cmd += ["-metadata", f"encoder={enc}"]
            cmd += ["-metadata", f"major_brand={brand}"]
            cmd += ["-metadata", f"minor_version={minor}"]
            cmd += [out_path]

            log(f"[{i+1}/3] Processando {ver['label']}...")
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode != 0:
                job['status'] = 'error'
                job['error'] = f"Erro na versao {ver['name']}: {result.stderr[-300:]}"
                return

            size = os.path.getsize(out_path)
            outputs.append({
                "name": ver['name'],
                "label": ver['label'],
                "filename": out_filename,
                "path": out_path,
                "size": size,
                "resolution": f"{out_w}x{out_h}",
            })
            job['progress'] = int((i+1) / 3 * 100)
            log(f"[OK] {ver['label']}: {out_filename} ({size//1024}KB)")

        job['status'] = 'done'
        job['outputs'] = outputs
        job['resolution_in'] = f"{w}x{h}"
        job['resolution_out'] = f"{out_w}x{out_h}"
        log("Concluido!")

    except Exception as e:
        job['status'] = 'error'
        job['error'] = str(e)


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return jsonify({'error': 'Nenhum arquivo'}), 400
    file = request.files['file']
    if not file or not allowed_file(file.filename):
        return jsonify({'error': 'Formato invalido'}), 400

    job_id = str(uuid.uuid4())[:8]
    original_name = secure_filename(file.filename)
    basename = os.path.splitext(original_name)[0]

    upload_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{job_id}_{original_name}")
    file.save(upload_path)

    output_dir = os.path.join(app.config['PROCESSED_FOLDER'], job_id)
    os.makedirs(output_dir, exist_ok=True)

    jobs[job_id] = {
        'id': job_id,
        'filename': original_name,
        'basename': basename,
        'status': 'queued',
        'progress': 0,
        'log': [],
        'outputs': [],
        'created_at': datetime.now().strftime("%d/%m/%Y %H:%M"),
    }

    thread = threading.Thread(target=process_video, args=(job_id, upload_path, basename, output_dir))
    thread.daemon = True
    thread.start()

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
    path = os.path.join(app.config['PROCESSED_FOLDER'], job_id, filename)
    if not os.path.exists(path):
        return jsonify({'error': 'Arquivo nao encontrado'}), 404
    return send_file(path, as_attachment=True)

@app.route('/jobs')
def list_jobs():
    return jsonify(list(jobs.values()))

if __name__ == '__main__':
    os.makedirs('uploads', exist_ok=True)
    os.makedirs('processed', exist_ok=True)
    app.run(host='0.0.0.0', port=5000, debug=False)
