with open(r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\ki-lime-pi-to-go\MCU_Module_RaspberryPi_Pico.kicad_sym', 'r', encoding='utf-8') as f:
    text = f.read()

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
lines = pico.split('\n')
for i in range(170, min(200, len(lines))):
    print(f"{i+1}: {lines[i].strip()}")
