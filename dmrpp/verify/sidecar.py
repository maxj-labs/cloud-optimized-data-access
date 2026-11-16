"""What the sidecar's XML says that VirtualiZarr's DMR++ parser does not pass on."""

from __future__ import annotations

from typing import Any

DAP4 = "{http://xml.opendap.org/ns/DAP/4.0#}"
DMRPP = "{http://xml.opendap.org/dap/dmrpp/1.0.0#}"


def sidecar_group_paths(element: Any, prefix: str = "") -> list[str]:
    """Paths of every group in the sidecar XML ("" for the root is left to the caller)."""
    out = []
    for group in element.findall(f"{DAP4}Group"):
        path = f"{prefix}/{group.get('name')}"
        out += [path, *sidecar_group_paths(group, path)]
    return out


def sidecar_notes(element: Any, prefix: str = "") -> dict[str, dict[str, bool]]:
    """{variable path: {big_endian, external, text_fill}} read from the sidecar XML itself.

    VirtualiZarr 2.8.0's DMR++ parser drops both: it reads every variable as
    little-endian whatever ``byteOrder`` says, and it reads every chunk from the
    data file even when the chunk's ``href`` puts it elsewhere. ``text_fill``
    marks a numeric variable whose ``_FillValue`` attribute is a string, which
    that parser cannot read."""
    out = {}
    for child in element:
        if child.tag == f"{DAP4}Group":
            out.update(sidecar_notes(child, f"{prefix}/{child.get('name')}"))
            continue
        chunks = child.find(f"{DMRPP}chunks")
        if chunks is None or child.get("name") is None:
            continue
        hrefs = {c.get("href") for c in chunks.iter(f"{DMRPP}chunk")} - {None, "OPeNDAP_DMRpp_DATA_ACCESS_URL"}
        text_fill = any(
            a.get("name") == "_FillValue" and a.get("type") == "String" for a in child.findall(f"{DAP4}Attribute")
        )
        out[f"{prefix}/{child.get('name')}"] = {
            "big_endian": chunks.get("byteOrder") == "BE",
            "external": bool(hrefs),
            "text_fill": text_fill and child.tag != f"{DAP4}String",
        }
    return out


def apply_sidecar_notes(
    index: dict[str, Any], meta: dict[str, dict[str, Any]], notes: dict[str, dict[str, bool]]
) -> int:
    """Drop variables stored outside the data file and set the declared byte order; returns how many were dropped."""
    external = [name for name, note in notes.items() if note["external"] and (name in index or name in meta)]
    for name in external:
        index.pop(name, None)
        meta.pop(name, None)
    for name, note in notes.items():
        if note["big_endian"] and name in meta:
            meta[name] = {**meta[name], "dtype": meta[name]["dtype"].replace("endianness='little'", "endianness='big'")}
    return len(external)


def sidecar_signs(text: bytes) -> dict[str, Any]:
    """How a sidecar's variable descriptions appear to have been produced, from its text alone.

    ``sidecar_cf_markers`` counts the ``fullnamepath`` attributes: older sidecars
    carry one per variable, newer ones none. The hypothesis under test is that
    those older sidecars are the ones that declare signed 8-bit data as Int16."""
    import re

    version = re.search(rb'dmrpp:version="([^"]+)"', text)
    return {
        "sidecar_version": version.group(1).decode() if version else "",
        "sidecar_cf_markers": text.count(b'name="fullnamepath"'),
        "sidecar_hdf5_global": b'name="HDF5_GLOBAL"' in text,
        "sidecar_int8_variables": len(re.findall(rb"<Int8 name=", text)),
    }
