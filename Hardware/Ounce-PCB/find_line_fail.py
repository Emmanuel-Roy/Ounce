import subprocess

sch_path = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\Ounce-PCB.kicad_sch'
test_pdf = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\test.pdf'
kicad_cli = r'C:\Program Files\KiCad\7.0\bin\kicad-cli.exe'

with open(r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\generate_complete_kicad7_schematic.py', 'r') as f:
    pass

# Run generator first
subprocess.run(['python', r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\generate_complete_kicad7_schematic.py'])

with open(sch_path, 'r', encoding='utf-8') as f:
    original_lines = f.readlines()

print(f"Loaded full schematic with {len(original_lines)} lines.")

for i in range(5, len(original_lines)+1):
    sub_lines = original_lines[4:i]
    test_str = "".join(sub_lines)
    open_c = test_str.count('(')
    close_c = test_str.count(')')
    if open_c > close_c:
        test_str_eval = test_str + ('\n)' * (open_c - close_c))
    else:
        test_str_eval = test_str
        
    sch_test = f"""(kicad_sch (version 20230121) (generator eeschema)
  (paper "A4")
  (lib_symbols
{test_str_eval}
  )
  (sheet_instances (path "/" (page "1")))
  (symbol_instances)
)
"""
    with open(sch_path, 'w', encoding='utf-8') as f:
        f.write(sch_test)

    res = subprocess.run([kicad_cli, 'sch', 'export', 'pdf', '-o', test_pdf, sch_path], capture_output=True, text=True)
    if res.returncode != 0:
        print(f"FAIL at line {i}: '{original_lines[i-1].strip()}'")
        break
    else:
        if i % 50 == 0 or i == len(original_lines):
            print(f"PASS up to line {i}")

# Restore full schematic
with open(sch_path, 'w', encoding='utf-8') as f:
    f.writelines(original_lines)
