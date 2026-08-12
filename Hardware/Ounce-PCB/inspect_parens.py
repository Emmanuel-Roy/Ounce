import re

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

pico = extract_symbol(sym_file, "RaspberryPi_Pico")

def convert_to_kicad7_lib_symbol(txt):
    txt = re.sub(r'\(version \d+\)', '(version 20220914)', txt)
    txt = re.sub(r'\s*\(do_not_autoplace\s+[^)]+\)', '', txt)
    txt = re.sub(r'\s*\(property "ki_[^"]*"[\s\S]*?\n\s*\)', '', txt)
    txt = re.sub(r'\s*\(alternate\s+[^)]+\)', '', txt)
    txt = re.sub(r'(\(pin\s+[^)]+\))\s+hide', r'\1', txt)
    txt = txt.replace('(effects (font (size 1.27 1.27))) hide', '(effects (font (size 1.27 1.27)) hide)')
    return txt

pico_lib = re.sub(r'^\(symbol "RaspberryPi_Pico"', '(symbol "MCU_Module_RaspberryPi_Pico:RaspberryPi_Pico"', pico)
pico_clean = convert_to_kicad7_lib_symbol(pico_lib)

lines = pico_clean.split('\n')
for i in range(10, 22):
    print(f"Line {i+1}: {lines[i]}")
