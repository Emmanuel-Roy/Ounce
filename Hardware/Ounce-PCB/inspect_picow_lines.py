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

picow = extract_symbol(sym_file, "RaspberryPi_Pico_W")

def convert_to_kicad7_lib_symbol(txt):
    txt = re.sub(r'\s*\(do_not_autoplace\s+[^)]+\)', '', txt)
    txt = re.sub(r'\s*\(in_bom\s+[^)]+\)', '', txt)
    txt = re.sub(r'\s*\(on_board\s+[^)]+\)', '', txt)
    txt = re.sub(r'\s*\(property "ki_[^"]*"[\s\S]*?\n\s*\)', '', txt)
    txt = re.sub(r'\s*\(alternate\s+[^)]+\)', '', txt)
    txt = re.sub(r'(\(pin\s+[^)]+\))\s+hide', r'\1', txt)
    txt = txt.replace('(effects (font (size 1.27 1.27))) hide', '(effects (font (size 1.27 1.27)) hide)')
    return txt

picow_clean = convert_to_kicad7_lib_symbol(picow)
lines = picow_clean.split('\n')
for i in range(min(25, len(lines))):
    print(f"{i+1}: {lines[i].strip()}")
