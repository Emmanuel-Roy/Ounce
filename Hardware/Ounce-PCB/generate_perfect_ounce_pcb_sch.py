import subprocess
import re
import uuid

sch_path = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\Ounce-PCB.kicad_sch'
test_pdf = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\test.pdf'
kicad_cli = r'C:\Program Files\KiCad\7.0\bin\kicad-cli.exe'

sym_path = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\ki-lime-pi-to-go\MCU_Module_RaspberryPi_Pico.kicad_sym'

with open(sym_path, 'r', encoding='utf-8') as f:
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

def remove_ki_properties(text):
    while True:
        idx = text.find('(property "ki_')
        if idx == -1:
            break
        count = 0
        end = idx
        for i in range(idx, len(text)):
            if text[i] == '(':
                count += 1
            elif text[i] == ')':
                count -= 1
                if count == 0:
                    end = i + 1
                    break
        text = text[:idx] + text[end:]
    return text

pico = extract_symbol(sym_file, "RaspberryPi_Pico")
picow = extract_symbol(sym_file, "RaspberryPi_Pico_W")

pico_lib = re.sub(r'^\(symbol "RaspberryPi_Pico"', '(symbol "MCU_Module_RaspberryPi_Pico:RaspberryPi_Pico"', pico)
picow_lib = re.sub(r'^\(symbol "RaspberryPi_Pico_W"', '(symbol "MCU_Module_RaspberryPi_Pico:RaspberryPi_Pico_W"', picow)
picow_lib = picow_lib.replace('(extends "RaspberryPi_Pico")', '(extends "MCU_Module_RaspberryPi_Pico:RaspberryPi_Pico")')

def convert_to_kicad7_lib_symbol(txt):
    txt = re.sub(r'\(version \d+\)', '(version 20220914)', txt)
    txt = re.sub(r'\s*\(do_not_autoplace\s+[^)]+\)', '', txt)
    txt = re.sub(r'\s*\(in_bom\s+[^)]+\)', '', txt)
    txt = re.sub(r'\s*\(on_board\s+[^)]+\)', '', txt)
    txt = remove_ki_properties(txt)
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

global_labels = []
def add_label(name, x, y, shape="bidirectional", angle=0):
    u = str(uuid.uuid4())
    global_labels.append(f"""  (global_label "{name}" (shape {shape}) (at {x:.2f} {y:.2f} {angle})
    (effects (font (size 1.27 1.27)) (justify left))
    (uuid "{u}")
  )""")

# Labels for U1 (Master)
add_label("SPI0_SCK", 73.66, 53.34, shape="input")
add_label("SPI0_MOSI", 73.66, 54.61, shape="input")
add_label("SPI0_CS0", 73.66, 57.15, shape="input")
add_label("SPI0_CS1", 73.66, 59.69, shape="input")
add_label("SPI0_CS2", 73.66, 62.23, shape="input")
add_label("SPI0_CS3", 73.66, 72.39, shape="input")
add_label("GND", 50.80, 101.60, shape="passive", angle=270)

# Labels for U2 (Slave 0)
add_label("SPI0_SCK", 137.16, 53.34, shape="input")
add_label("SPI0_MOSI", 137.16, 55.88, shape="input")
add_label("SPI0_CS0", 137.16, 58.42, shape="input")
add_label("GND", 114.30, 101.60, shape="passive", angle=270)

# Labels for U3 (Slave 1)
add_label("SPI0_SCK", 200.66, 53.34, shape="input")
add_label("SPI0_MOSI", 200.66, 55.88, shape="input")
add_label("SPI0_CS1", 200.66, 58.42, shape="input")
add_label("GND", 177.80, 101.60, shape="passive", angle=270)

# Labels for U4 (Slave 2)
add_label("SPI0_SCK", 264.16, 53.34, shape="input")
add_label("SPI0_MOSI", 264.16, 55.88, shape="input")
add_label("SPI0_CS2", 264.16, 58.42, shape="input")
add_label("GND", 241.30, 101.60, shape="passive", angle=270)

# Labels for U5 (Slave 3)
add_label("SPI0_SCK", 327.66, 53.34, shape="input")
add_label("SPI0_MOSI", 327.66, 55.88, shape="input")
add_label("SPI0_CS3", 327.66, 58.42, shape="input")
add_label("GND", 304.80, 101.60, shape="passive", angle=270)

full_sch = f"""(kicad_sch (version 20230121) (generator eeschema) (generator_version "7.0")
  (uuid "64149908-1f33-4118-b113-679e16cf28e6")
  (paper "A4")
  (lib_symbols
{pico_clean}
{picow_clean}
  )
{chr(10).join(symbol_elements)}
{chr(10).join(global_labels)}
  (sheet_instances (path "/" (page "1")))
  (symbol_instances
{chr(10).join(symbol_instances)}
  )
)
"""

with open(sch_path, 'w', encoding='utf-8') as f:
    f.write(full_sch)

res = subprocess.run([kicad_cli, 'sch', 'export', 'pdf', '-o', test_pdf, sch_path], capture_output=True, text=True)
print("Final schematic generation exit code:", res.returncode)
print("stdout:", res.stdout)
print("stderr:", res.stderr)
