"""亚洲语歌预匹配（零依赖，纯启发式）。

用于备用链路的**静默**排序决策：命中（任一即算）时，本次按
“咪咕优先 + 维基共享/互联网档案馆垫底且按需检索”执行；
未命中则维持用户已配置的音源顺序，不做任何额外动作。

规则：
1. 任意文本里含汉字（CJK 统一表意文字）**或假名**（日语歌适用同一排序：
   实测咪咕对 J-pop 覆盖不错，而 CC/PD 库几乎只有西语/古典）；
2. 歌手文本命中内置的常见华语歌手名单（含常用英文/拼音写法）。

只做“倾向”判断、不求完备：判错的代价只是源顺序微调或省/多一次
低命中源的检索，不参与任何正确性校验，也不会打扰用户。
"""
from __future__ import annotations

import re

from . import jp

__all__ = ["POPULAR_ARTISTS", "contains_han", "is_chinese_likely"]

_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

#: 常见华语热门歌手（含常用英文/拼音写法）；大小写不敏感，只用于“倾向”判断。
POPULAR_ARTISTS = frozenset(x.casefold() for x in (
    "周杰伦", "Jay Chou", "林俊杰", "JJ Lin", "陈奕迅", "Eason Chan",
    "邓紫棋", "G.E.M.", "薛之谦", "李荣浩", "毛不易", "华晨宇", "张杰",
    "汪苏泷", "许嵩", "周深", "邓丽君", "王菲", "那英", "张学友", "刘德华",
    "郭富城", "黎明", "蔡依林", "Jolin Tsai", "孙燕姿", "Stefanie Sun",
    "梁静茹", "Fish Leong", "张韶涵", "杨丞琳", "王心凌", "张惠妹", "A-Mei",
    "莫文蔚", "刘若英", "田馥甄", "Hebe Tien", "五月天", "Mayday", "苏打绿",
    "Sodagreen", "告五人", "凤凰传奇", "TFBOYS", "时代少年团", "朴树", "李健",
    "汪峰", "赵雷", "陈粒", "逃跑计划", "张国荣", "梅艳芳", "谭咏麟", "李克勤",
    "容祖儿", "杨千嬅", "Twins", "S.H.E", "任贤齐", "陶喆", "David Tao",
    "王力宏", "Leehom Wang", "方大同", "Khalil Fong", "萧敬腾", "Jam Hsiao",
    "林宥嘉", "Yoga Lin", "徐佳莹", "Lala Hsu", "单依纯", "张碧晨", "许巍",
    "李宗盛", "罗大佑", "周华健", "张信哲", "陈绮贞", "戴佩妮", "A-Lin",
    "光良", "品冠", "曹格", "陈慧琳", "苏有朋", "吴青峰", "韩红", "孙楠",
    "腾格尔", "毛阿敏", "周传雄", "阿杜", "许美静", "彭佳慧", "徐怀钰",
    "张宇", "伍佰", "动力火车", "信乐团", "张震岳", "飞儿乐团", "Beyond",
))


def contains_han(text: str) -> bool:
    """文本中是否含汉字（CJK 统一表意文字，含扩展 A 与兼容区）。"""
    return bool(text) and bool(_HAN.search(text))


def is_chinese_likely(*texts: str) -> bool:
    """任意输入文本呈“华语歌”倾向则返回 True（启发式，见模块 docstring）。"""
    joined = " ".join(t for t in texts if t)
    if not joined:
        return False
    # 汉字、假名、以及「可完整罗马音化」的输入（yorushika / zankoku na tenshi no teze）
    # 都按亚洲语处理：咪咕优先、CC/PD 垫底 —— 少打扰两个慢速开放库，实测更省时。
    if _HAN.search(joined) or jp.looks_japanese(joined):
        return True
    norm = joined.casefold()
    return any(artist in norm for artist in POPULAR_ARTISTS)
