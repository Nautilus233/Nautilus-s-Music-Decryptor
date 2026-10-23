# 内置表来源与许可

本目录下的数据文件由 `tools/build_jp_tables.py` 联网构建、可复跑，运行时只读本地文件。

* `jp_words.tsv` — 派生自 **JMdict**（Electronic Dictionary Research and Development Group，via jmdict-simplified），许可 **CC BY-SA 4.0**。
* `jp_songs.tsv` / `jp_artists.tsv` — 读音抓取自 **日语维基百科**（CC BY-SA 4.0，API 首段/信息框）；少量条目使用 JMdict 回退读音。罫线：只收录事实性读音，不收录歌词等受版权保护的文本。
* `cjk_confusions.tsv` / `cjk_words.txt` — 派生自 **pycorrector**（Apache-2.0）的`same_pinyin` / `same_stroke` / `word_freq` / `common_char_set` 数据文件。
* `album_hanzi.tsv` — 派生自 [OpenCC ver.1.1.9 TSCharacters.txt](https://github.com/BYVoid/OpenCC/blob/ver.1.1.9/data/dictionary/TSCharacters.txt)，仅保留无歧义的单字符条目；用于专辑字形比对与有限补查，不改显示名/标签，不引入 OpenCC 运行库。许可 Apache-2.0，完整许可见 `OpenCC-LICENSE.txt`；原文件 SHA-256 记录在表头。
* `album_aliases.json` — 人工核实的 Wikidata 专辑实体事实（CC0）。《幻燈》对应 Q117788597，P31=专辑、P175=ヨルシカ；译名只用于实体查找，不能证明发行版本或曲目完整性。
  《夏草が邪魔をする》对应 Q86734884（P31=EP、P175=ヨルシカ、P2635=7）；原名及英文名取该实体标签，简称仅对唯一前缀生成检索，不把简称当官方别名。
