import subprocess

sch_path = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\minimal.kicad_sch'
test_pdf = r'Z:\Code\Github\Ounce\Hardware\Ounce-PCB\test_min.pdf'
kicad_cli = r'C:\Program Files\KiCad\7.0\bin\kicad-cli.exe'

min_sch = """(kicad_sch (version 20230121) (generator eeschema) (generator_version "7.0")
  (uuid "64149908-1f33-4118-b113-679e16cf28e6")
  (paper "A4")
  (lib_symbols
  )
  (sheet_instances (path "/" (page "1")))
  (symbol_instances)
)
"""

with open(sch_path, 'w', encoding='utf-8') as f:
    f.write(min_sch)

res = subprocess.run([kicad_cli, 'sch', 'export', 'pdf', '-o', test_pdf, sch_path], capture_output=True, text=True)
print("Bare minimal schematic export exit code:", res.returncode)
print("stdout:", res.stdout)
print("stderr:", res.stderr)
