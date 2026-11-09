# Test fixtures

`json-text_homo-dei-ducebatur.json` and `json-cid_006868.json` are real Cantus
Index responses (November 2026). The json-cid response is abridged: it keeps the
complete `info` block and the per-database counts, but only the first two of 139
concordance records, so code reading it must report the evidence as incomplete.

`json-feasts_sample.json` is made up, following the documented json-feasts
fields; the feast codes and dates are illustrative only.

Tests serve these with a UTF-8 byte-order mark in front, as Cantus Index does.

`synthetic_edition.html` is a made-up fragment in the markup of the Nasjonalbiblioteket
edition (span.red rubrics, span.pageRef pages, paraCenter headings). It exercises the
parser's vigil, Second Vespers and post-Second Vespers rules; the texts are illustrative.
