import os
import tempfile
import subprocess
from flask import Flask, request, send_file, render_template_string

app = Flask(__name__)
INDEX_HTML = """
<!doctype html>
<title>Auto-accept prep rules - upload</title>
<h2>Upload Excel (order-level)</h2>
<form method=post enctype=multipart/form-data>
  <input type=file name=file accept=".xlsx,.xls" required>
  <br><br>
  <label>Venue (optional): <input type=text name=venue></label>
  <br><br>
  <button type=submit>Upload & Generate CSV</button>
</form>
"""

@app.route("/", methods=["GET","POST"])
def index():
    if request.method == "GET":
        return render_template_string(INDEX_HTML)
    f = request.files.get("file")
    if not f:
        return "No file", 400
    venue = request.form.get("venue") or ""
    tmpdir = tempfile.mkdtemp()
    inpath = os.path.join(tmpdir, "input.xlsx")
    outpath = os.path.join(tmpdir, "out.csv")
    f.save(inpath)
    cmd = ["python3", "process_prep.py", "--input", inpath, "--out", outpath]
    if venue:
        cmd += ["--venue", venue]
    subprocess.check_call(cmd)
    return send_file(outpath, as_attachment=True, download_name="prep_rules.csv")

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
