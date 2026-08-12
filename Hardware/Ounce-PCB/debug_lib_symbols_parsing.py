import subprocess
import re

sch_path = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\Ounce-PCB.kicad_sch'
test_pdf = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\test.pdf'
kicad_cli = r'C:\Program Files\KiCad\7.0\bin\kicad-cli.exe'

with open(r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\ki-lime-pi-to-go\MCU_Module_RaspberryPi_Pico.kicad_sym', 'r', encoding='utf-8') as f:
    sym_file = f.read()

def extract_symbol(text, sym_name):
    target = f'(symbol "{sym_name}"'
    idx = text.find(target)
    if idx == -1:
        return ""
    count = 0
    start = idx
    for i in range(idx, len(text)):
        if text[i] == '(':
            count += 1
        elif text[i] == ')':
            count -= 1
            if count == 0:
                return text[start:i+1]
    return ""

# Fix hide inside effects
sym_file = sym_file.replace('(effects (font (size 1.27 1.27))) hide', '(effects (font (size 1.27 1.27)) hide)')

pico = extract_symbol(sym_file, "RaspberryPi_Pico")
pico = re.sub(r'^\(symbol "RaspberryPi_Pico"', '(symbol "MCU_Module_RaspberryPi_Pico:RaspberryPi_Pico"', pico)

lines = pico.split('\n')

for i in range(1, len(lines)+1):
    sub_lines = lines[:i]
    test_str = "\n".join(sub_lines)
    open_c = test_str.count('(')
    close_c = test_str.count(')')
    if open_c > close_c:
        test_str += ('\n)' * (open_c - close_c))
        
    sch_test = f"""(kicad_sch (version 20230121) (generator eeschema)
  (paper "A4")
  (lib_symbols
{test_str}
  )
  (sheet_instances (path "/" (page "1")))
  (symbol_instances)
)
"""
    with open(sch_path, 'w', encoding='utf-8') as f:
        f.write(sch_test)

    res = subprocess.run([kicad_cli, 'sch', 'export', 'pdf', '-o', test_pdf, sch_path], capture_output=True, text=True)
    if res.returncode != 0:
        print(f"FAIL at line {i}: '{lines[i-1].strip()}'")
        break
    else:
        if i % 50 == 0 or i == len(lines):
            print(f"PASS up to line {i}")
