import subprocess
import re
import uuid

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

pico = extract_symbol(sym_file, "RaspberryPi_Pico")

# Replace ALL occurrences of "RaspberryPi_Pico with "MCU_Module_RaspberryPi_Pico:RaspberryPi_Pico
pico_lib = re.sub(r'\(symbol "([^"]+)"', r'(symbol "MCU_Module_RaspberryPi_Pico:\1"', pico)

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

symbols_info = [
    ('U1', 'MCU_Module_RaspberryPi_Pico:RaspberryPi_Pico', 'RaspberryPi_Pico', 50.8, 63.5),
    ('U2', 'MCU_Module_RaspberryPi_Pico:RaspberryPi_Pico', 'RaspberryPi_Pico', 114.3, 63.5),
    ('U3', 'MCU_Module_RaspberryPi_Pico:RaspberryPi_Pico', 'RaspberryPi_Pico', 177.8, 63.5),
    ('U4', 'MCU_Module_RaspberryPi_Pico:RaspberryPi_Pico', 'RaspberryPi_Pico', 241.3, 63.5),
    ('U5', 'MCU_Module_RaspberryPi_Pico:RaspberryPi_Pico', 'RaspberryPi_Pico', 304.8, 63.5),
]

symbol_elements = []
symbol_instances = []

for ref, lib_id, val, x, y in symbols_info:
    u = str(uuid.uuid4())
    symbol_instances.append(f"""    (path "/{u}"
      (reference "{ref}")
      (unit 1)
    )""")
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

sch_test_all = f"""(kicad_sch (version 20230121) (generator eeschema) (generator_version "7.0")
  (uuid "64149908-1f33-4118-b113-679e16cf28e6")
  (paper "A4")
  (lib_symbols
{pico_clean}
  )
{chr(10).join(symbol_elements)}
  (sheet_instances (path "/" (page "1")))
  (symbol_instances
{chr(10).join(symbol_instances)}
  )
)
"""

with open(sch_path, 'w', encoding='utf-8') as f:
    f.write(sch_test_all)

res = subprocess.run([kicad_cli, 'sch', 'export', 'pdf', '-o', test_pdf, sch_path], capture_output=True, text=True)
print("Subsymbol prefix test exit code:", res.returncode)
print("stdout:", res.stdout)
print("stderr:", res.stderr)
