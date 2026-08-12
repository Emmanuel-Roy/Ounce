import re

sym_path = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\ki-lime-pi-to-go\MCU_Module_RaspberryPi_Pico.kicad_sym'

with open(sym_path, 'r', encoding='utf-8') as f:
    text = f.read()

# Fix 1: KiCad 7 version header
text = re.sub(r'\(version \d+\)', '(version 20220914)', text)
text = re.sub(r'\(generator_version "[^"]+"\)', '(generator_version "7.0")', text)

# Remove KiCad 8+ tags
text = re.sub(r'\s*\(do_not_autoplace\s+[^)]+\)', '', text)
text = re.sub(r'\s*\(in_bom\s+[^)]+\)', '', text)
text = re.sub(r'\s*\(on_board\s+[^)]+\)', '', text)
text = re.sub(r'\s*\(alternate\s+[^)]+\)', '', text)
text = re.sub(r'(\(pin\s+[^)]+\))\s+hide', r'\1', text)

with open(sym_path, 'w', encoding='utf-8') as f:
    f.write(text)

print("Cleaned ki-lime-pi-to-go/MCU_Module_RaspberryPi_Pico.kicad_sym on disk.")
