#!/bin/sh
# A tour of Liton on a real binary built right here.
#
#   sh examples/demo.sh
#
# It builds three tiny applications with the system compiler, then shows how
# Liton analyses them, remembers a recipe for each, and reuses it on the second
# run without doing the work again.
set -e

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
work=${TMPDIR:-/tmp}/liton-demo
rm -rf "$work"
mkdir -p "$work/app/lib"

LITON="python3 -m liton"
export PYTHONPATH="$here/.."
export LITON_HOME="$work/liton-home"

command -v gcc >/dev/null 2>&1 || { echo "this demo needs gcc"; exit 1; }

# --- a plain application --------------------------------------------------
printf '#include <stdio.h>\n#include <math.h>\n' > "$work/hello.c"
printf 'int main(void){printf("hello, sqrt(2)=%%g\\n",sqrt(2.0));return 0;}\n' >> "$work/hello.c"
gcc -O2 "$work/hello.c" -o "$work/hello" -lm

# --- an application that ships a private library --------------------------
printf 'const char* greet(void){return "greeting from a bundled library";}\n' > "$work/app/lib/libgreet.c"
gcc -shared -fPIC "$work/app/lib/libgreet.c" -o "$work/app/lib/libgreet.so" -Wl,-soname,libgreet.so
printf '#include <stdio.h>\nconst char* greet(void);\n' > "$work/app/greeter.c"
printf 'int main(void){printf("%%s\\n",greet());return 0;}\n' >> "$work/app/greeter.c"
gcc "$work/app/greeter.c" -o "$work/app/greeter" \
    -L"$work/app/lib" -lgreet -Wl,-rpath,'$ORIGIN/lib'

# --- an application that needs a library we then remove -------------------
mkdir -p "$work/fake"
printf 'void SDL_Init(void){}\nvoid SDL_CreateWindow(void){}\nvoid SDL_PollEvent(void){}\n' > "$work/sdl.c"
gcc -shared -fPIC "$work/sdl.c" -o "$work/fake/libSDL2-2.0.so.0" -Wl,-soname,libSDL2-2.0.so.0
printf '#include <stdio.h>\nvoid SDL_Init(void);\n' > "$work/game.c"
printf 'int main(void){SDL_Init();printf("the game ran\\n");return 0;}\n' >> "$work/game.c"
gcc "$work/game.c" -o "$work/game" -L"$work/fake" -l:libSDL2-2.0.so.0 -Wl,-rpath,"$work/fake"
rm -rf "$work/fake"

hr() { printf '\n=== %s ===\n' "$1"; }

hr "1. a plain application: analyse, remember, run"
$LITON translate "$work/hello"
$LITON run "$work/hello"
$LITON run "$work/hello"          # second run reuses the profile

hr "2. an application with a private library: the runpath is remembered"
$LITON analyze "$work/app/greeter" | python3 -c \
  'import json,sys;d=json.load(sys.stdin);print("runpath:",d["elf"]["runpath"])'
$LITON run "$work/app/greeter"

hr "3. an application with a missing library: the package is identified"
$LITON deps "$work/game"

hr "4. what Liton remembered"
$LITON list

hr "5. verify a remembered profile"
$LITON verify "$work/app/greeter" --execute

echo
echo "profiles and artifacts live in: $LITON_HOME"
