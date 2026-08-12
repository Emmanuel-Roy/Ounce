import subprocess

sch_path = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\Ounce-PCB.kicad_sch'
test_pdf = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\test.pdf'
kicad_cli = r'C:\Program Files\KiCad\7.0\bin\kicad-cli.exe'

with open(sch_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

print(f"Total lines: {len(lines)}")

# Calculate exact line numbers where top level elements (depth 1 -> 0 or 1 -> 1) start/end
depth = 0
element_end_lines = []

for idx, line in enumerate(lines):
    line_num = idx + 1
    open_c = line.count('(')
    close_c = line.count(')')
    depth += (open_c - close_c)
    if depth == 1 and line.strip() == ')':
        element_end_lines.append(line_num)

print(f"Element end lines: {element_end_lines}")

for check_line in element_end_lines:
    sub_lines = lines[:check_line]
    test_str = "".join(sub_lines)
    test_str += "\n  (sheet_instances (path \"/\" (page \"1\")))\n  (symbol_instances)\n)"
    with open(sch_path, 'w', encoding='utf-8') as f:
        f.write(test_str)
    
    res = subprocess.run([kicad_cli, 'sch', 'export', 'pdf', '-o', test_pdf, sch_path], capture_output=True, text=True)
    if res.returncode != 0:
        print(f"FAIL at element end line {check_line}: '{lines[check_line-1].strip()}'")
        break
    else:
        print(f"PASS up to element end line {check_line}")

with open(sch_path, 'w', encoding='utf-8') as f:
    f.writelines(lines)
