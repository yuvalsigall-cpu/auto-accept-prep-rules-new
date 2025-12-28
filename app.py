import streamlit as st
import pandas as pd
import tempfile
import subprocess
import os

st.title("Auto-Accept Prep Rules")

st.write("העלה קובץ אקסל ותקבל CSV מוכן")

uploaded_file = st.file_uploader("Upload Excel", type=["xlsx"])
venue = st.text_input("Venue (אופציונלי)")

if uploaded_file is not None:
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, "input.xlsx")
        output_path = os.path.join(tmpdir, "output.csv")

        with open(input_path, "wb") as f:
            f.write(uploaded_file.getbuffer())

        if st.button("Generate CSV"):
            cmd = ["python", "process_prep.py", "--input", input_path, "--out", output_path]
            if venue:
                cmd += ["--venue", venue]

            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode != 0:
                st.error("שגיאה בהרצה")
                st.code(result.stderr)
            else:
                st.success("הקובץ מוכן!")
                with open(output_path, "rb") as f:
                    st.download_button(
                        "Download CSV",
                        f,
                        file_name="prep_rules.csv",
                        mime="text/csv"
                    )
