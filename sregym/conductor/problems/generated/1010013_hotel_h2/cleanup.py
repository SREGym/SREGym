import pathlib,shutil,sys
path=pathlib.Path(sys.argv[1])
assert path.name.startswith(('.publication.','.recovery.'))
shutil.rmtree(path)
