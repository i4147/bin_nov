which files have a code similar to this

try:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=output_path.parent,
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)
        json.dump(report, temp_file, indent=2)
        temp_file.write("\n")
    os.replace(temp_path, output_path)
finally:
    if temp_path is not None:
        temp_path.unlink(missing_ok=True)

i wanna replace them with atomic_write from dh
also check for any function similar to gsz and fsz from dh
if found, they should be removed, and be imported from dh
