# sidecar_assets

Vendored copy of `darktable-mcp/sidecar/{segment.py,matte.py,requirements.txt,requirements-torch.txt}`,
shipped as package data inside the `darktable_mcp` package (and therefore
inside the `darktable-mcp` .deb's bundled venv) so `darktable-mcp
install-sidecar` (see `darktable_mcp/cli/install_sidecar.py`) can lay down a
working SAM2 + MODNet sidecar checkout WITHOUT the user needing a separate
git clone/copy step -- only the venv build (`uv venv` + the pinned pip
installs) and the two checkpoint downloads (SAM2 ~176MB, MODNet ~24.7MB)
need network access, all handled by `install-sidecar` itself.

`requirements.txt` pins BOTH sam2 (needs the torch step first) and
onnxruntime (MODNet's only inference dependency, no torch needed) -- see its
own header comment for the install order.

These files are small (~35KB total) and pure Python/text -- unlike the
sidecar's OWN `.venv` (torch CPU + two checkpoints), copying them costs
nothing meaningful in package size. They intentionally stay separate from
`sidecar/` (which build-deb-mcp.sh still excludes from the vendored source
tree) so the "no torch/checkpoints bundled" decision is unaffected.

Keep these in sync with `darktable-mcp/sidecar/` by hand if that source
changes (no build-time symlink/copy step exists yet -- see
`install_sidecar.py`'s module docstring).
