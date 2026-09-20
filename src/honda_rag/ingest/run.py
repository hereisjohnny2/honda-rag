"""Orquestrador da ingestão. Etapas idempotentes e retomáveis.

    python -m honda_rag.ingest.run --stage ocr   [--pages 25-110] [--force]
    python -m honda_rag.ingest.run --stage footer               # relê o rodapé (rótulos das páginas)
    python -m honda_rag.ingest.run --stage load                 # estrutura + specs + chunks no Postgres
    python -m honda_rag.ingest.run --stage embed                # embeddings dos chunks pendentes
    python -m honda_rag.ingest.run --stage all
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

import requests
from PIL import Image

from honda_rag import config, llm
from honda_rag.db import repo
from honda_rag.ingest import chunk as C
from honda_rag.ingest import extract as E
from honda_rag.ingest import footer, ocr, rasterize, specs_table, structure as S
from honda_rag.ingest.normalize import normalize

SPEC_TABLE_PAGES = range(42, 54)


def parse_pages(spec: str | None) -> list[int]:
    return config._pages(spec) if spec else [p for p in config.PILOT_PAGES if p >= config.FIRST_SCAN_PAGE]


def sha256_file(path: Path) -> str:
    cache = path.with_suffix(".sha256")
    if cache.exists():
        return cache.read_text().strip()
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    cache.write_text(h.hexdigest())
    return h.hexdigest()


def _label_key(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in s.split("-"))


def _merge_micro(procs: list[S.Procedure]) -> list[tuple[str, S.Procedure]]:
    """Títulos quase vazios (só um subtítulo do sumário) viram trilha de migalhas do próximo."""
    out, crumbs = [], []
    for i, p in enumerate(procs):
        n_text = sum(1 for b in p.blocks if b.kind == "text")
        last = i == len(procs) - 1
        if n_text < 4 and not last and procs[i + 1].pdf_start == p.pdf_start:
            crumbs.append(p.title)
            continue
        out.append((" › ".join(dict.fromkeys(crumbs + [p.title])), p))
        crumbs = []
    return out


def load(pages: list[int]) -> None:
    pages = [p for p in pages if (rasterize.page_dir(p) / "ocr.json").exists()]
    labels = S.infer_labels(pages)
    procs = _merge_micro(S.build_procedures(pages))
    sha = sha256_file(config.PDF_PATH)

    with repo.connect() as conn:
        repo.reset_document(conn, sha)
        doc_id = conn.execute(
            """INSERT INTO documents (title, doc_type, model_years, engines, file_path, file_sha256)
               VALUES (%s,'service_manual','[1992,1996)',%s,%s,%s) RETURNING id""",
            ("Honda Civic 1992-1995 Service Manual", config.ENGINES,
             config.PDF_PATH.relative_to(config.ROOT).as_posix(), sha)).fetchone()[0]

        # ---- seções, sumário, páginas
        sec_ids: dict[int, int] = {}
        starts = sorted(S.SECTION_STARTS.items())
        for i, (start, num) in enumerate(starts):
            end = starts[i + 1][0] - 1 if i + 1 < len(starts) else max(pages)
            sec_ids[num] = conn.execute(
                "INSERT INTO sections (document_id, number, title, pdf_page_start, pdf_page_end) "
                "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (doc_id, str(num), S.SECTION_TITLES[num], start, end)).fetchone()[0]

        toc_ids: dict[int, int] = {}
        for e in S.load_toc():
            sec = S.section_of(e["pdf_page"])
            toc_ids[e["order"]] = conn.execute(
                "INSERT INTO toc_entries (document_id, ord, title, pdf_page, section_id) "
                "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (doc_id, e["order"], e["title"], e["pdf_page"], sec_ids.get(sec))).fetchone()[0]

        page_ids: dict[int, int] = {}
        for p in pages:
            o = S.load_ocr(p)
            text = "\n".join(b.text for b in S.page_blocks(p))
            needs = o["mean_conf"] < 70
            d = rasterize.page_dir(p)
            page_ids[p] = conn.execute(
                """INSERT INTO pages (document_id, pdf_page, page_label, section_id, image_path, view_path,
                       ocr_text, ocr_engine, ocr_confidence, layout_json, needs_review)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,'tesseract',%s,%s,%s) RETURNING id""",
                (doc_id, p, labels.get(p), sec_ids.get(S.section_of(p)),
                 (d / "page.png").relative_to(config.ROOT).as_posix(), (d / "view.webp").relative_to(config.ROOT).as_posix(),
                 text, o["mean_conf"], repo.jb(o["layout"]), needs)).fetchone()[0]
            if needs:
                conn.execute("INSERT INTO review_queue (item_type,item_id,reason,candidates) "
                             "VALUES ('page',%s,'low_conf',%s)",
                             (page_ids[p], repo.jb({"pdf_page": p, "conf": o["mean_conf"]})))

        # ---- procedimentos e chunks de texto
        page_main_proc: dict[int, int] = {}
        proc_rows: list[tuple[int, str, S.Procedure]] = []
        for title, pr in procs:
            blocks = pr.blocks
            if not blocks:
                continue
            full = C.procedure_full_text(blocks)
            pages_cov = sorted({b.page for b in blocks})
            plabels = sorted({labels[p] for p in pages_cov if labels.get(p)}, key=_label_key)
            steps = S.extract_steps(blocks)
            ptype = S.procedure_type(title.split(" › ")[-1])
            pid = conn.execute(
                """INSERT INTO procedures (section_id, toc_entry_id, component, procedure_type, title, steps,
                       warnings, consumables, applicability, page_labels, full_text)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (sec_ids.get(S.section_of(pr.pdf_start)), toc_ids.get(pr.toc_order), title.split(" › ")[-1],
                 ptype, title, repo.jb(steps), S.extract_warnings(blocks), S.extract_consumables(blocks),
                 repo.jb(S.applicability(full)), plabels, full)).fetchone()[0]
            proc_rows.append((pid, title, pr))
            for p in pages_cov:
                cnt = sum(1 for b in blocks if b.page == p)
                if cnt > page_main_proc.get(-p, 0):
                    page_main_proc[p], page_main_proc[-p] = pid, cnt

            section = f"Section {S.section_of(pr.pdf_start)} {S.SECTION_TITLES.get(S.section_of(pr.pdf_start), '')}".strip()
            title_eng = S.applicability(title).get("engine", [])
            sticky = title_eng
            ctype = ("flowchart" if "flowchart" in title.lower() else
                     "spec_table" if ptype == "specs" else "procedure")
            sec_no = S.section_of(pr.pdf_start)
            chunks = C.split_chunks(
                blocks, labels,
                lambda labs, t=title: C.make_prefix(section, " / ".join(t.split(" › ")[-2:]), [], labs))
            for ch in chunks:
                heads = [S.ENGINE_RE.findall(pg) for pg in ch["body"].split("\n")
                         if re.match(r"^(D\w{4}(,\s*)?)+\s+engine", pg, re.I)]
                if heads:
                    engs = sorted({e for h in heads for e in h})
                    sticky = sorted(set(heads[-1]))
                else:
                    engs = sticky
                text = ch["text"]
                if engs:  # o prefixo carrega o motor: ajuda o retrieval lexical e o LLM
                    text = text.replace("]\n", " · " + "/".join(engs) + "]\n", 1)
                conn.execute(
                    "INSERT INTO chunks (procedure_id, chunk_type, text, page_labels, applicability) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (pid, ctype, text, ch["labels"] or ["?"], repo.jb({"engine": engs} if engs else {})))

        # ---- especificações: tabelas + torques
        n_spec = 0
        spec_rows: list[tuple[int, dict]] = []
        for p in pages:
            if p in SPEC_TABLE_PAGES:
                specs, issues = specs_table.parse_page(S.load_ocr(p), p)
                for sp in specs:
                    sp["page_label"] = labels.get(p)
                    spec_rows.append((p, sp))
                for iss in issues:
                    conn.execute("INSERT INTO review_queue (item_type,item_id,reason,candidates) "
                                 "VALUES ('spec',NULL,%s,%s)", (iss["reason"], repo.jb({**iss["candidates"], "page": p})))
        for p in pages:
            if p in SPEC_TABLE_PAGES:
                continue
            main = next((t for pid, t, pr in proc_rows if page_main_proc.get(p) == pid), "")
            main_name = main.replace(" › ", " / ")
            specs, issues = E.torque_specs(p, labels.get(p), main_name)
            for sp in specs:
                cond = []
                if sp.get("step"):
                    cond.append(f"step {sp['step'][0]}: {sp['step'][1]}")
                if sp.get("near_label") and not sp.get("step") and not sp["near_label"].startswith("("):
                    cond.append(f"near {sp['near_label'].title()}")
                if sp["component"] == main_name and sp["fastener"]:
                    sp["component"] = f"{sp['component']} — fastener {sp['fastener']}"
                sp["conditions"] = " | ".join(cond) or None
                spec_rows.append((p, sp))
            for iss in issues:
                conn.execute("INSERT INTO review_queue (item_type,item_id,reason,candidates) "
                             "VALUES ('spec',NULL,%s,%s)", (iss["reason"], repo.jb({**iss["candidates"], "page": p})))
        for p, sp in spec_rows:
            engines = sp.get("engines") or []
            conn.execute(
                """INSERT INTO specs (document_id, component, parameter, value_text, value_min, value_max, unit,
                       fastener, side, conditions, applicability, page_id, procedure_id, source_engine, verified)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ocr',FALSE)""",
                (doc_id, sp["component"], sp["parameter"], sp["value_text"], sp.get("value_min"),
                 sp.get("value_max"), sp.get("unit"), sp.get("fastener"), sp.get("side"), sp.get("conditions"),
                 repo.jb({"engine": engines} if engines else {}), page_ids[p], page_main_proc.get(p)))
            n_spec += 1

        # chunks de spec (tabelas agrupadas por página/componente; torques por página)
        by_key: dict[tuple[int, str], list[dict]] = {}
        for p, sp in spec_rows:
            grp = sp["component"].split(":")[0] if sp["parameter"] != "torque" else "Torque"
            by_key.setdefault((p, grp), []).append(sp)
        for (p, grp), rows in by_key.items():
            lab = labels.get(p)
            sec = S.section_of(p)
            prefix = C.make_prefix(f"Section {sec} {S.SECTION_TITLES.get(sec, '')}".strip(),
                                   "Torque values" if grp == "Torque" else "Standards and Service Limits",
                                   [], [lab] if lab else [])
            for txt in C.spec_chunk_texts(rows, prefix):
                engs = sorted({e for r in rows for e in r.get("engines", [])})
                conn.execute(
                    "INSERT INTO chunks (procedure_id, chunk_type, text, page_labels, applicability) "
                    "VALUES (%s,'spec_table',%s,%s,%s)",
                    (page_main_proc.get(p), txt, [lab or "?"], repo.jb({"engine": engs} if engs else {})))

        # ---- ferramentas especiais, part numbers, DTC
        for p in pages:
            for t in E.special_tools(p):
                conn.execute(
                    "INSERT INTO special_tools (document_id, tool_number, name, page_id) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING", (doc_id, t["tool_number"], t["name"], page_ids[p]))
                conn.execute(
                    "INSERT INTO chunks (procedure_id, chunk_type, text, page_labels, applicability) "
                    "VALUES (%s,'text',%s,%s,'{}')",
                    (page_main_proc.get(p),
                     f"[Section 6 Cylinder Head/Valve Train · Special Tools · p. {labels.get(p)}]\n"
                     f"Special tool {t['tool_number']}: {t['name']}. Used on pages {t['page_ref']}.",
                     [labels.get(p) or "?"]))
        all_blocks = [b for p in pages for b in S.page_blocks(p)]
        for pn in E.part_numbers(all_blocks):
            conn.execute("INSERT INTO part_numbers (document_id, part_number, description, page_id) "
                         "VALUES (%s,%s,%s,%s)", (doc_id, pn["part_number"], pn["description"], page_ids[pn["page"]]))
        for d in E.dtc_codes(all_blocks):
            system = re.search(r"in the (.+?) circuit", d["description"])
            proc = page_main_proc.get(d["page"])
            conn.execute("INSERT INTO dtc_codes (document_id, code, system, description, procedure_id) "
                         "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                         (doc_id, d["code"], system.group(1) if system else None, d["description"], proc))

        # ---- figuras (recortes; legenda/callouts da VLM na fase 4)
        n_fig = _save_figures(conn, pages, page_ids, procs)
        conn.commit()

        counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in ("pages", "procedures", "chunks", "specs", "special_tools", "part_numbers",
                            "dtc_codes", "figures", "review_queue")}
    print("carga concluída:", counts)


def _save_figures(conn, pages: list[int], page_ids: dict[int, int], procs) -> int:
    proc_of_page: dict[int, str] = {}
    for title, pr in procs:
        for b in pr.blocks:
            proc_of_page.setdefault(b.page, title)
    n = 0
    config.FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    for p in pages:
        o = S.load_ocr(p)
        figs = o["layout"].get("figures", [])
        if not figs:
            continue
        img = Image.open(rasterize.page_dir(p) / "page.png")
        title = proc_of_page.get(p, "").lower()
        ftype = ("flowchart" if "flowchart" in title else
                 "exploded_view" if "illustrated index" in title else "procedure_illustration")
        for i, (x0, y0, x1, y1) in enumerate(figs):
            pad = 30
            box = (max(0, x0 - pad), max(0, y0 - pad), min(img.width, x1 + pad), min(img.height, y1 + pad))
            path = config.FIGURES_DIR / f"p{p:04d}_{i}.png"
            img.crop(box).save(path)
            conn.execute("INSERT INTO figures (page_id, bbox, image_path, figure_type) VALUES (%s,%s,%s,%s)",
                         (page_ids[p], list(box), path.relative_to(config.ROOT).as_posix(), ftype))
            n += 1
    return n


# ------------------------------------------------------------------ embeddings
def embed_texts(texts: list[str]) -> list[list[float]]:
    return llm.embed(texts, kind="document", cpu=False)   # ingestão: GPU liberada para o Ollama


def embed(batch: int = 16) -> None:
    with repo.connect() as conn:
        rows = conn.execute("SELECT id, text FROM chunks WHERE embedding IS NULL OR embedding_model IS DISTINCT "
                            "FROM %s ORDER BY id", (config.EMBED_ID,)).fetchall()
        print(f"embeddings pendentes: {len(rows)} (modelo: {config.EMBED_ID})")
        t0 = time.time()
        for i in range(0, len(rows), batch):
            part = rows[i:i + batch]
            vecs = embed_texts([t for _, t in part])
            for (cid, _), v in zip(part, vecs):
                conn.execute("UPDATE chunks SET embedding=%s::vector, embedding_model=%s WHERE id=%s",
                             (repo.vec(v), config.EMBED_ID, cid))
            conn.commit()
            print(f"  {min(i + batch, len(rows))}/{len(rows)}  ({time.time() - t0:.0f}s)", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["rasterize", "ocr", "footer", "load", "embed", "all"], required=True)
    ap.add_argument("--pages", help="ex.: 25-110,120")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    pages = parse_pages(a.pages)
    if a.stage == "rasterize":
        rasterize.run(pages, a.force, a.workers)
    if a.stage in ("ocr", "all"):
        ocr.run(pages, a.force, a.workers)
    if a.stage in ("footer", "all"):
        footer.run(pages, a.force, a.workers)
    if a.stage in ("load", "all"):
        load(pages)
    if a.stage in ("embed", "all"):
        embed()


if __name__ == "__main__":
    main()
