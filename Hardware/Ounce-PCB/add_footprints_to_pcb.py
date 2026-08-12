import subprocess
import re
import uuid

pcb_path = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\Ounce-PCB.kicad_pcb'
fp_path = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\ki-lime-pi-to-go\Module_RaspberryPi_Pico.pretty\RaspberryPi_Pico_Common.kicad_mod'
kicad_cli = r'C:\Program Files\KiCad\7.0\bin\kicad-cli.exe'

with open(fp_path, 'r', encoding='utf-8') as f:
    fp_text = f.read()

# Clean KiCad 8 tags from footprint string if any
def clean_footprint(txt):
    txt = re.sub(r'\(version \d+\)', '(version 20221018)', txt)
    txt = re.sub(r'\s*\(do_not_autoplace\s+[^)]+\)', '', txt)
    txt = re.sub(r'\s*\(in_bom\s+[^)]+\)', '', txt)
    txt = re.sub(r'\s*\(on_board\s+[^)]+\)', '', txt)
    return txt

fp_clean = clean_footprint(fp_text)

# Convert footprint header from (footprint "RaspberryPi_Pico_Common" ... to (footprint "Module_RaspberryPi_Pico:RaspberryPi_Pico_Common" ...
fp_lib_mod = re.sub(r'^\(footprint "RaspberryPi_Pico_Common"', '(footprint "Module_RaspberryPi_Pico:RaspberryPi_Pico_Common"', fp_clean)

picos_pcb = [
    ('U1', 'RaspberryPi_Pico_W', 150.0, 100.0, 0),
    ('U2', 'RaspberryPi_Pico', 98.0, 100.0, 180),
    ('U3', 'RaspberryPi_Pico', 124.0, 100.0, 180),
    ('U4', 'RaspberryPi_Pico', 176.0, 100.0, 180),
    ('U5', 'RaspberryPi_Pico', 202.0, 100.0, 180),
]

footprint_elements = []

for ref, val, x, y, rot in picos_pcb:
    u = str(uuid.uuid4())
    mod_instance = fp_lib_mod
    # Update position / rotation at top-level of footprint
    mod_instance = re.sub(r'\(footprint "Module_RaspberryPi_Pico:RaspberryPi_Pico_Common" (\(layer "[^"]+"\))',
                         f'(footprint "Module_RaspberryPi_Pico:RaspberryPi_Pico_Common" \\1 (at {x:.2f} {y:.2f} {rot}) (tstamp "{u}")',
                         mod_instance)
    
    # Update Reference property
    mod_instance = re.sub(r'\(property "Reference" "REF\*\*"', f'(property "Reference" "{ref}"', mod_instance)
    # Update Value property
    mod_instance = re.sub(r'\(property "Value" "RaspberryPi_Pico_Common"', f'(property "Value" "{val}"', mod_instance)
    
    footprint_elements.append(mod_instance)

pcb_full = f"""(kicad_pcb (version 20221018) (generator pcbnew)
  (general
    (thickness 1.6)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (40 "Dwgs.User" user "User.Drawings")
    (41 "Cmts.User" user "User.Comments")
    (42 "Eco1.User" user "User.Eco1")
    (43 "Eco2.User" user "User.Eco2")
    (44 "Edge.Cuts" user)
    (45 "Margin" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (gr_line (start 82.5 69.5) (end 217.5 69.5) (layer "Edge.Cuts") (width 0.1) (tstamp "83f6b4e1-2e63-4b68-8092-2b634bf16b11"))
  (gr_line (start 217.5 69.5) (end 217.5 130.5) (layer "Edge.Cuts") (width 0.1) (tstamp "83f6b4e1-2e63-4b68-8092-2b634bf16b12"))
  (gr_line (start 217.5 130.5) (end 82.5 130.5) (layer "Edge.Cuts") (width 0.1) (tstamp "83f6b4e1-2e63-4b68-8092-2b634bf16b13"))
  (gr_line (start 82.5 130.5) (end 82.5 69.5) (layer "Edge.Cuts") (width 0.1) (tstamp "83f6b4e1-2e63-4b68-8092-2b634bf16b14"))
{chr(10).join(footprint_elements)}
)
"""

with open(pcb_path, 'w', encoding='utf-8') as f:
    f.write(pcb_full)

res = subprocess.run([kicad_cli, 'pcb', 'export', 'svg', '--layers', 'F.Cu,F.SilkS,Edge.Cuts', pcb_path], capture_output=True, text=True)
print("PCB SVG export exit code:", res.returncode)
print("stdout:", res.stdout)
print("stderr:", res.stderr)
