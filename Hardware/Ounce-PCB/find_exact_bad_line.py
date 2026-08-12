import subprocess

sch_path = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\Ounce-PCB.kicad_sch'
test_pdf = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\test.pdf'
kicad_cli = r'C:\Program Files\KiCad\7.0\bin\kicad-cli.exe'

with open(sch_path, 'r', encoding='utf-8') as f:
    original_lines = f.readlines()

print(f"Total lines in schematic: {len(original_lines)}")

for i in range(18, len(original_lines)+1):
    sub_lines = original_lines[:i]
    test_str = "".join(sub_lines)
    open_c = test_str.count('(')
    close_c = test_str.count(')')
    if open_c > close_c:
        test_str_eval = test_str + ('\n)' * (open_c - close_c))
    else:
        test_str_eval = test_str
        
    if '(sheet_instances' not in test_str_eval:
        test_str_eval += '\n  (sheet_instances (path "/" (page "1")))\n  (symbol_instances)\n)'
    elif '(symbol_instances)' not in test_str_eval:
        test_str_eval += '\n  (symbol_instances)\n)'

    with open(sch_path, 'w', encoding='utf-8') as f:
        f.write(test_str_eval)
    
    res = subprocess.run([kicad_cli, 'sch', 'export', 'pdf', '-o', test_pdf, sch_path], capture_output=True, text=True)
    if res.returncode != 0:
        print(f"FAIL at line {i}: '{original_lines[i-1].strip()}'")
        break
    else:
        if i % 50 == 0 or i == len(original_lines):
            print(f"PASS up to line {i}")

with open(sch_path, 'w', encoding='utf-8') as f:
    f.writelines(original_lines)
