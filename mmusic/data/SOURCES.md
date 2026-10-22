# 内置表来源与许可

本目录下的数据文件由 `tools/build_jp_tables.py` 联网构建、可复跑，运行时只读本地文件。

* `jp_words.tsv` — 派生自 **JMdict**（Electronic Dictionary Research and Development Group，via jmdict-simplified），许可 **CC BY-SA 4.0**。
* `jp_songs.tsv` / `jp_artists.tsv` — 读音抓取自 **日语维基百科**（CC BY-SA 4.0，API 首段/信息框）；少量条目使用 JMdict 回退读音。罫线：只收录事实性读音，不收录歌词等受版权保护的文本。
* `cjk_confusions.tsv` / `cjk_words.txt` — 派生自 **pycorrector**（Apache-2.0）的`same_pinyin` / `same_stroke` / `word_freq` / `common_char_set` 数据文件。
