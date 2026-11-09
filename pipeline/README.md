# pipeline

Scripts that turn the Nasjonalbiblioteket HTML edition into a master workbook for import
into the database:

```
python pipeline/parse_breviarium.py EDITION.html --outdir out [--start-heading ...] [--end-heading ...]
python pipeline/build_xlsx.py out master.xlsx --label "..."
python -m app import master.xlsx --source brev
```

`breviarium_pilot_QA.md` records the checks made on the pilot (pp. 553-577).

## Feast parts and Second Vespers

The edition does not always print a heading where a new liturgical day begins. The parser
supplies one in square brackets, taken from the printed heading, and flags the heading line
`FEAST_SUPPLIED`:

* After a vigil heading (IN UIGILIA ...), the first Vespers begins the feast proper:
  IN UIGILIA SANCTI ANDREE APOSTOLI is followed by [SANCTI ANDREE APOSTOLI].
* Matins after Second Vespers begins a further office (an octave or a following day):
  [SANCTI ANDREE APOSTOLI, post secundas vesperas].

Chants in a supplied part get the note "Feast heading not printed in the edition; supplied"
in the new `extra_note` column, which goes into the import file's "extra" column after the
rubric. The bracketed headings are only keys: each one is mapped to its Cantus feast
(Vigilia Andreae, Andreae, Andreae,8 or Octava Andreae ...) with `python -m app map-feast`.

An "Ad vesperas" without primas/secundas is coded V2 when Lauds or a day hour of the same
feast has already appeared (flag `OFFICE_V2_INFERRED`), and V otherwise (`OFFICE_V_UNSPEC`).

Incipit-only items are still resolved against earlier texts under the same *printed*
heading, so a cue in a supplied part can find its full text in the part before it.

## Superseded

`enrich_cids.py` and `export_import.py` are kept for reference only. Cantus ID candidates and
decisions are now handled by the app (`fetch`, `decide`), and the import file will be
exported from the database. (`export_import.py` also calls `import_rows_from_master`, which
this version of the parser does not define.)
