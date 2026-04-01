import glob
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

from flask import jsonify, request, send_file


def register_workspace_misc_routes(app, server):
    @app.route("/api/workspace/download")
    def workspace_download():
        file_path = request.args.get("path", "")
        if not file_path:
            return jsonify({"error": "Missing path"}), 400

        file_path = os.path.abspath(file_path)
        base_dir = os.path.abspath(server.base_dir)
        allowed_dirs = [
            os.path.join(base_dir, "data", "workspace"),
            os.path.join(base_dir, "data", "web"),
            os.path.join(base_dir, "static", "files"),
        ]

        if not any(file_path.startswith(os.path.abspath(d)) for d in allowed_dirs):
            return jsonify({"error": "Access denied"}), 403

        if not os.path.exists(file_path) or not os.path.isfile(file_path):
            return jsonify({"error": "File not found"}), 404

        return send_file(
            file_path,
            as_attachment=True,
            download_name=os.path.basename(file_path),
        )

    @app.route("/api/workspace/convert/pptx-to-pdf", methods=["POST"])
    def convert_pptx_to_pdf():
        if "file" not in request.files:
            return jsonify({"error": "Missing file"}), 400

        file = request.files["file"]
        if not file.filename or not file.filename.lower().endswith(".pptx"):
            return jsonify({"error": "Only .pptx files are supported"}), 400

        cache_dir = os.path.join(server.base_dir, "data", "workspace", "pptx_cache")
        os.makedirs(cache_dir, exist_ok=True)

        def get_file_hash(file_path):
            hash_md5 = hashlib.md5()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_md5.update(chunk)
            return hash_md5.hexdigest()

        def get_soffice_path():
            windows_paths = [
                r"C:\Program Files\LibreOffice\program\soffice.exe",
                r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
                os.path.expanduser(r"~\AppData\Local\Programs\LibreOffice\program\soffice.exe"),
            ]

            program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
            program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
            for pf in [program_files, program_files_x86]:
                matches = glob.glob(os.path.join(pf, "LibreOffice*", "program", "soffice.exe"))
                if matches:
                    windows_paths.insert(0, matches[0])

            for name in ["soffice", "soffice.exe"]:
                soffice_in_path = shutil.which(name)
                if soffice_in_path:
                    return soffice_in_path

            if sys.platform == "win32":
                for candidate in windows_paths:
                    if os.path.exists(candidate):
                        return candidate

            return "soffice"

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                input_path = os.path.join(tmpdir, "input.pptx")
                file.save(input_path)

                file_hash = get_file_hash(input_path)
                cached_pdf_path = os.path.join(cache_dir, f"{file_hash}.pdf")
                if os.path.exists(cached_pdf_path):
                    return send_file(cached_pdf_path, mimetype="application/pdf")

                soffice_cmd = get_soffice_path()
                cmd_args = [
                    soffice_cmd,
                    "--headless",
                    "--norestore",
                    "--nofirststartwizard",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    tmpdir,
                    input_path,
                ]
                result = subprocess.run(
                    cmd_args,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )

                pdf_path = os.path.join(tmpdir, "input.pdf")
                if result.returncode == 0 and os.path.exists(pdf_path):
                    shutil.copy2(pdf_path, cached_pdf_path)
                    return send_file(pdf_path, mimetype="application/pdf")

                return jsonify(
                    {
                        "error": "PDF conversion failed",
                        "detail": result.stderr or "LibreOffice conversion failed",
                    }
                ), 500
        except subprocess.TimeoutExpired:
            return jsonify({"error": "Conversion timed out"}), 500
        except FileNotFoundError:
            return jsonify(
                {
                    "error": "LibreOffice not found",
                    "detail": "Install LibreOffice and ensure soffice is available",
                }
            ), 500
        except Exception as e:
            return jsonify({"error": str(e)}), 500
