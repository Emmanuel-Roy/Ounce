import re
import subprocess

sch_path = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\Ounce-PCB.kicad_sch'
test_pdf = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\test.pdf'
kicad_cli = r'C:\Program Files\KiCad\7.0\bin\kicad-cli.exe'

with open(r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\ki-lime-pi-to-go\MCU_Module_RaspberryPi_Pico.kicad_sym', 'r', encoding='utf-8') as f:
    text = f.read()

# Strip hide from pin definitions
text = re.sub(r'(\(pin\s+[^)]+\))\s+hide', r'\1', text)

with open(r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\ki-lime-pi-to-go\MCU_Module_RaspberryPi_Pico.kicad_sym', 'w', encoding='utf-8') as f:
    f.write(text)

def extract_symbol(txt, sym_name):
    target = f'(symbol "{sym_name}"'
    idx = txt.find(target)
    if idx == -1:
        return ""
    count = 0
    start = idx
    for i in range(idx, len(txt)):
        if txt[i] == '(':
            count += 1
        elif txt[i] == ')':
            count -= 1
            if count == 0:
                return txt[start:i+1]
    return ""

pico = extract_symbol(text, "RaspberryPi_Pico")
picow = extract_symbol(text, "RaspberryPi_Pico_W")

pico_lib = re.sub(r'^\(symbol "RaspberryPi_Pico"', '(symbol "MCU_Module_RaspberryPi_Pico:RaspberryPi_Pico"', pico)
picow_lib = re.sub(r'^\(symbol "RaspberryPi_Pico_W"', '(symbol "MCU_Module_RaspberryPi_Pico:RaspberryPi_Pico_W"', picow)

def convert_to_kicad7_lib_symbol(txt):
    txt = re.sub(r'\s*\(do_not_autoplace\s+[^)]+\)', '', txt)
    txt = re.sub(r'\s*\(in_bom\s+[^)]+\)', '', txt)
    txt = re.sub(r'\s*\(on_board\s+[^)]+\)', '', txt)
    txt = re.sub(r'\s*\(property "ki_[^"]*"[\s\S]*?\n\s*\)', '', txt)
    txt = re.sub(r'\s*\(alternate\s+[^)]+\)', '', txt)
    txt = re.sub(r'(\(pin\s+[^)]+\))\s+hide', r'\1', txt)
    txt = txt.replace('(effects (font (size 1.27 1.27))) hide', '(effects (font (size 1.27 1.27)) hide)')
    return txt

pico_clean = convert_to_kicad7_lib_symbol(pico_lib)
picow_clean = convert_to_kicad7_lib_symbol(picow_lib)

symbols_info = [
    ('U1', 'MCU_Module_RaspberryPi_Pico:RaspberryPi_Pico_W', 'RaspberryPi_Pico_W', 50.8, 63.5),
    ('U2', 'MCU_Module_RaspberryPi_Pico:RaspberryPi_Pico', 'RaspberryPi_Pico', 114.3, 63.5),
    ('U3', 'MCU_Module_RaspberryPi_Pico:RaspberryPi_Pico', 'RaspberryPi_Pico', 177.8, 63.5),
    ('U4', 'MCU_Module_RaspberryPi_Pico:RaspberryPi_Pico', 'RaspberryPi_Pico', 241.3, 63.5),
    ('U5', 'MCU_Module_RaspberryPi_Pico:RaspberryPi_Pico', 'RaspberryPi_Pico', 304.8, 63.5),
]

symbol_elements = []

for ref, lib_id, val, x, y in symbols_info:
    u = "11111111-1111-1111-1111-11111111111" + ref[-1]
    symbol_elements.append(f"""  (symbol (lib_id "{lib_id}") (at {x:.2f} {y:.2f} 0) (unit 1)
    (in_bom yes) (on_board yes) (uuid "{u}")
    (property "Reference" "{ref}" (at {x:.2f} {y-40.64:.2f} 0)
      (effects (font (size 1.27 1.27)) (justify left))
    )
    (property "Value" "{val}" (at {x:.2f} {y-38.1:.2f} 0)
      (effects (font (size 1.27 1.27)) (justify left))
    )
    (property "Footprint" "Module_RaspberryPi_Pico:RaspberryPi_Pico_Common" (at {x:.2f} {y+40.64:.2f} 0)
      (effects (font (size 1.27 1.27)) (justify left) hide)
    )
  )""")

full_sch = f"""(kicad_sch (version 20230121) (generator eeschema) (generator_version "7.0")
  (uuid "64149908-1f33-4118-b113-679e16cf28e6")
  (paper "A4")
  (lib_symbols
{pico_clean}
{picow_clean}
  )
{chr(10).join(symbol_elements)}
  (sheet_instances
    (path "/" (page "1"))
  )
  (symbol_instances)
)
"""

with open(sch_path, 'w', encoding='utf-8') as f:
    f.write(full_sch)

res = subprocess.run([kicad_cli, 'sch', 'export', 'pdf', '-o', test_pdf, sch_path], capture_output=True, text=True)
print("Complete schematic (with clean lib_symbols) exit code:", res.returncode)
print("stdout:", res.stdout)
print("stderr:", res.stderr)
