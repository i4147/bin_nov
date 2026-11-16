i updated my cprint module (from dh) -> i have a repo named dh, check it, its all there
the new module has this signature:
cprint('text',color='cyan')
from dh import Ansi, Cube, cprint, cstr, cube, gray

# 1) Basic named colors
cprint("Hello world", color="red")
cprint("Warning!", color=Ansi.BRIGHT_YELLOW, bold=True)
cprint("Done", color="green", bg="black")

# 2) ANSI 256 cube — direct coordinate or preset enum
cprint("cube red",  color=cube(5, 0, 0))   # 196
cprint("cube blue", color=Cube.BLUE)       # 21
cprint("orange",    color=Cube.ORANGE)     # 214

# 3) Grayscale + RGB tuple (auto-mapped to nearest 256 color)
cprint("dim note", color=gray(8))          # 240
cprint("pure red", color=(255, 0, 0))

# 4) Rich-style strings, drop-in for print()
cprint("hex",      color="#ff8800")
cprint("cube()",   color="cube(3,3,0)")
cprint("gray()",   color="gray(12)")
cprint("multi", 1, 2, 3, sep=" - ", color="cyan", underline=True)

# 5) Get the ANSI string back instead of printing
line = cstr("embedded red", color="red")
print(repr(line))


please inspect the .py files in root of repo, and fix any cprint usage to new style
update files inplace
report stats when done

in short:
you should find : "green"   "blue"   and other colors and add `color=` before them
