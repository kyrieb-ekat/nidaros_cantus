
# CLAUDE.md: Nidaros chant import (Breviarium / Missale Nidrosiense → Cantus Database)

Last updated 2026-11-18. Owner: Kyrie Bouressa (DDMAL, McGill).

## Working with Kyrie

- Ask before generating code. Ask Kyrie when something is unclear. Review everything three times (professional, correct, elegant).
- Never add information to prose Kyrie asks for help with; flag where information may be missing.
- Say plainly what is verified and what is inferred. Do not edit shared documents beyond fixing my own errors unless asked.
- Kyrie runs the CLI scripts locally (conda env `nidaros`). Claude's sandbox cannot reach cantusindex.org or cantusdatabase.org.

## Goal

Turn the Nasjonalbiblioteket (2019) HTML editions of the Nidaros breviary (printed 1519) and missal into spreadsheets that import into the Cantus Database (CDB). Pilot: Breviary, Vigil of St Andrew to Lucy, edition pp. 553–577 (565 lines, 319 import rows). The Missal is not started: it needs its HTML and the CDB Mass genre codes (check cantusdatabase.org/genres first).

## Source markup (NB HTML)

- `span.red` = rubric; untagged text = sung/read text; `span.pageRef` = `[553 | dd.viij.]` (sample images suggest edition page = facsimile page; signature printed on rectos only).
- Signs Ⱥ ℟ ℣ sit inside red spans. `<sup>` points to endnotes `lemma] reading breviarium`: lemma = edition reading, reading = the print. `‹ ›` = editorial supplied letters. Hymn stanzas are `div.stanza`.
- Page markers can sit anywhere in a line, even inside rubric lines. Update the running page after every block (a bug here put 48 rows on the wrong page; fixed).
- `aa.v.`-style references are leaf references (signature + leaf), resolved by leaf + incipit. Single-letter ones (`a.vj.`) are unexplained (psalm tone?); copied to notes, not interpreted.

## Files (all in the outputs folder)

- `parse_breviarium.py`: HTML → master rows (+ `import.csv`). `build_xlsx.py`: master → review workbook. The workbook layout is the interchange format.
- `nidaros.sqlite` is Kyrie's live working system: `line`, `candidate`, `decision`, `current_decision` (view), `feast_map`, `flag`, `spot_check`, `edit_log`, `api_cache`, `line_export` (view). `origin_ref` = "master_seq N"; `line_on_page` counts in tens. Never write to it without a backup and a dry run.
- `fix_pages_nidaros_db.py`: corrects page fields from the master (guarded, logged).
- `sync_decisions_to_workbook.py`: database → workbook, one way, never overwrites silently.
- `export_import.py`: workbook → import CSV. Its `feast` column holds the printed heading, so for CDB use the database export (maps feasts via `feast_map`).
- `enrich_cids.py`: proposes CIDs from Cantus Index (`json-text`, replies `[{cid, fulltext, genre}]` with a BOM; decode utf-8-sig). The queries cached in the database use wildcard patterns (e.g. `h_c %est _%er%e`), which seem to cope with spelling variants better than mine.

## Rules that must hold

- Never assign a Cantus ID automatically. Only decision Confirm or Fixed puts a CID into `cantus_id`. Variant, New CID and Reject stay statuses.
- Keep nothing out of the master: lessons, prayers, rubrics and headings stay; the import is chants only (A, R, V, W, H, HV, I).
- CDB genres: A antiphon, R responsory, V responsory verse, W versicle, H hymn, HV hymn verse, I invitatory; Cap, Or, Co tagged but not imported. Services: V first vespers, V2 second, M, L, P, T, S, N, C, D.
- `fulltext_ms` = print reading (endnote variants applied, no punctuation, `|` marks the repetendum). Incipit-only items get `*` on the standardized text only.
- Rubrics go in `extra` in the import file. Page = `folio`.

## Known gaps and next steps

1. **Standardized spelling is provisional and partly wrong.** Cantus Index restores ae/oe (que→quae) and uses **j** for consonantal i (eius→ejus); the parser does the opposite for j. A lexicon built from CI texts raised word agreement from 76% to 84% (leave-one-out, 141 lines). Also strip the repetendum cue from responsory-verse standardized text. Not implemented; Kyrie has not yet chosen between fixing the parser or a script that updates `fulltext_standardized` in the database.
2. 18 lines have no office (NO_OFFICE); 61 carry POSITION_CHECK; 3 import lines are undecided (master_seq 30, 430, 485); 35 spot-check rows are unreviewed.
3. Feasts: *Sequitur de sancta Anna* (December) has no CI feast (unmapped, 64 lines); *Victoricus et Fustianus* is mapped provisionally to *Fusciani, Gentiani* (14121110), which omits Victoricus (10 lines). A request document exists; Kyrie is passing it to a colleague, who will also review the duplicate-CID items. Dates and timing notes are Kyrie's to write.
4. Missal: HTML and Mass genre codes needed before any parsing.
