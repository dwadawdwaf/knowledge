# RAGFlow lexical dictionaries

Source: InfiniFlow RAGFlow v0.24.0, commit `392ec99651da78f6a8e1a2f60fe4818a95a8651d`.
The unmodified resources are `rag/res/ner.json` and `rag/res/synonym.json`. See the included Apache License 2.0.

`ragflow_native_query.py` adapts the same release's `FulltextQueryer.question`,
`common/query_base.py`, and token preprocessing/merge rules in `rag/nlp/term_weight.py`.
Platform imports, Redis, logging of queries and NumPy-only arithmetic are omitted;
dictionary, tokenizer and IDF are provided by the project's local adapters.
The OpenSearch mapping retains the upstream `conf/mapping.json` scripted similarity
for `*_tks`, default BM25 for `*_ltks`, and whitespace token analysis.

`ragflow_tokenizer.py` adapts `infinity/rag_tokenizer.py` from the exact
`infinity-sdk==0.7.0.dev2` wheel pinned by this RAGFlow release. Its Apache
copyright notice is retained. The local resource preparer extracts the matching
`infinity/huqie.txt` dictionary from the verified upstream wheel.
