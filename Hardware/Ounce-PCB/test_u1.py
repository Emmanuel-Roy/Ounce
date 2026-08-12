import subprocess

sch_path = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\Ounce-PCB.kicad_sch'
test_pdf = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\test.pdf'
kicad_cli = r'C:\Program Files\KiCad\7.0\bin\kicad-cli.exe'

with open(sch_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

print(f"Total lines: {len(lines)}")

check_line = 255
sub_lines = lines[:check_line]
test_str = "".join(sub_lines)
test_str += "\n  (sheet_instances (path \"/\" (page \"1\")))\n  (symbol_instances\n    (path \"/aae6cb27-de8d-40ec-86c9-8bda17f17924\" (reference \"U1\") (unit 1))\n  )\n)"

with open(sch_path, 'w', encoding='utf-8') as f:
    f.write(test_str)

res = subprocess.run([kicad_cli, 'sch', 'export', 'pdf', '-o', test_pdf, sch_path], capture_output=True, text=True)
print(f"U1 test exit code: {res.returncode}")
print("stdout:", res.stdout)
print("stderr:", res.stderr)

with open(sch_path, 'w', encoding='utf-8') as f:
    f.writelines(lines)
