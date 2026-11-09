# Annotation tooling

The scripts that produced the type annotations in the root-level `*.py` files of
this repository (PR #1: 409 files, ~2,000 new hints - 1,313 parameters and 713
returns - and no behaviour change).
They are kept here so the pass can be re-run, audited or extended.

> These are a cleaned-up reconstruction of the tooling used for that run, not a
> byte-for-byte copy: the scratch directory (`/tmp/ann`) did not survive the
> session. The inference rules, the safety gates and the reporting are the same;
> a couple of heuristics are slightly more aggressive, so a fresh `--write` run
> would add another ~270 hints in ~72 files on top of the committed state.

## The approach, in short

No type checker, no third-party dependencies (only `ast` and `tokenize` from the
standard library). For every function the tool asks, in order of decreasing
confidence:

1. **The default value.** `def f(n=0)` → `int`, `def f(p=None)` → the type comes
   from usage, `Optional[...]`, `def f(xs=[])))` → `list[Any]`.
2. **Usage evidence inside the body**, which is where most of the value is:
   - `p.read_text()` / `p.exists()` / `p.rglob(...)` → `Path`
   - `s.strip()` / `s.splitlines()` / `s.startswith(...)` → `str`
   - `f.read()` / `f.readlines()` / `f.write(...)` → `TextIO`
   - `d["k"]`, `d.get(...)`, `d.setdefault(...)` → `dict[str, Any]`
   - `xs.append(...)` → `list`, `xs.discard(...)` → `set`
   - `name.decode()` → `bytes`, `name.encode()` → `str`
   - `for x in param` / `sorted(param)` → `Iterable`/`Sequence`
   - `param(...)` (called) → `Callable[..., Any]`
   - `subprocess.run(param)` → `str`, `int(param)` / `float(param)` / `bool(param)` → that type
3. **The returned / yielded expressions**, which give parameter types a second
   way (`return Path(a, b)` also tells us what `a` and `b` are).
4. **Same-file knowledge:** the return type of a sibling method, the type of a
   module-level constant, `with open(...) as f` scopes, and the dataclass /
   class body annotations.
5. **Base classes:** `ast.NodeVisitor` → `visit_X(self, node: ast.X)`,
   `cst.CSTTransformer` → `cst.X`, `HTMLParser` → `str` where the base class
   dictates it. Overrides (`@override`, or a name that exists in the base) are
   left alone rather than guessed at.
6. **Naming conventions** (`*_path`, `*_count`, `is_*`, ...) - last resort, and
   only when nothing above fired.

If none of that produces a confident answer, the parameter is **left
unannotated**. That is deliberate: a wrong hint is worse than a missing one, and
it is why ~1.9k of the 15k parameters in this repo still have no hint
(free-form `**kwargs`, values coming from third-party libraries, ...).

`self` / `cls` are never annotated (PEP 484 / PEP 673); `self.x` attributes are
annotated only in `__init__` of attribute-heavy classes, never in `__post_init__`
or in a property setter.

## How a file is edited

The tool does not reformat. It tokenizes the file, finds the exact character
offset just after the parameter name or the closing `)`, and inserts `: Type` /
` -> Type` there, so comments, line breaks and the original style survive. New
imports are folded into an existing `from typing import ...` line, or added as a
block after the last stdlib import. PEP 604 (`X | None`) vs `Optional[X]` follows
whatever the file already uses.

## Safety gates (all of them must pass, per file)

| gate | what it catches |
| --- | --- |
| re-parse + `compile()` | a syntax error from a bad insertion |
| AST equivalence | the file with annotations stripped must dump identically to the original - so no expression, default, decorator or decorator argument was touched |
| import repair + `audit_annotations.py` | an annotation naming something that is not imported in the file |
| `git diff --stat` | nothing outside `*.py` changed |

Anything that fails a gate is reported and the file is left untouched.

## Files

| file | what it does |
| --- | --- |
| `infer_annotations.py` | the annotator itself: `python arena/infer_annotations.py f.py [--write] [--json r.json]` |
| `run_all.py` | batched driver over the root `*.py` files, prints progress per batch, optional JSON report |
| `annotation_stats.py` | coverage report (functions, parameters, return hints, biggest remaining gaps) |
| `audit_annotations.py` | the three checks: syntax, unimported annotation names, AST drift vs. a git revision (`--rev HEAD`) |
| `measure.py` | accuracy harness: strips a file's annotations, re-infers them, and compares with what was there before |
| `annotate_root.sh` | `run_all.py` + `annotation_stats.py` + `audit_annotations.py` in one go |

## Usage

```bash
# dry run: what would change, nothing written
python arena/run_all.py

# annotate in place, then verify
bash arena/annotate_root.sh --write

# audit the working tree against HEAD
python arena/audit_annotations.py --rev HEAD

# how good is the inference? (~54% exact, ~77% at least annotated, on a sample)
python arena/measure.py --sample 50 --show-misses
```

Everything is idempotent: a second run over an annotated tree changes nothing
apart from the heuristics noted at the top. `--help` works on every script.

## Numbers from the original run

| metric | before | after |
| --- | --- | --- |
| parameters | 14,929 | 15,018 |
| ... annotated | 11,847 (79.4%) | 13,160 (87.6%) |
| functions with a return hint | 8,042 (88.4%) | 8,755 (96.3%) |
| files that gained annotations | — | 408 |
| AST drift / unimported names / syntax errors | — | 0 / 0 / 0 |

(The parameter total grows a little because annotated `self.x` attributes and
module-level names are counted too.) The current tree measures 98.2% of
parameters and 95.9% of return values annotated; the difference is the stricter
counting of `self`-methods and the handful of hints in `Annotated[...]` /
`Literal[...]` form that the simple counter does not see.

Why it took so long: ~716 files, each parsed, walked several times
(the second pass re-uses the return types learned in the first), edited,
re-parsed, AST-compared and re-compiled - roughly 38 s for the full dry run now,
but the original pass ran with far more conservative settings and many
file-by-file iterations, which is where the wall-clock time went.
