#!/usr/bin/env python3
"""Standalone script to export a PDF (with annotations) from reMarkable via rmapi."""
import os
import subprocess
from dotenv import load_dotenv

load_dotenv()

# Path to the file on the reMarkable, relative to the root.
REMARKABLE_PDF_PATH = "<folder_path>/<your_pdf_file.pdf>"

# Directory to save the exported PDF to.
OUTPUT_DIR = "data"

def export_pdf(remarkable_path, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    rmapi_host = os.getenv('RMAPI_HOST') #e.g. https://remarkable.example.com, for a self-hosted rmfakecloud instance
    if rmapi_host:
        os.environ['RMAPI_HOST'] = rmapi_host
    # geta pulls the PDF with annotations baked in and writes '<name>-annotations.pdf' to cwd
    command = f'rmapi geta "/{remarkable_path}"'
    print(command)
    subprocess.check_call(command, shell=True, cwd=output_dir)

if __name__ == "__main__":
    export_pdf(REMARKABLE_PDF_PATH, OUTPUT_DIR)
