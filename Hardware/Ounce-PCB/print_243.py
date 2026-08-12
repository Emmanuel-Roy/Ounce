with open(r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\Ounce-PCB.kicad_sch', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i in range(230, 250):
    if i < len(lines):
        print(f"Line {i+1}: {lines[i].rstrip()}")
