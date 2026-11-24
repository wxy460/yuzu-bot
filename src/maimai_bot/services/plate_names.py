"""Normalize version-plate spelling without changing unrelated collectible titles."""

import re
import unicodedata

# Official achievement plate spellings use Japanese shinjitai: 双, 暁 and 将, but 鏡, 櫻 and 極.
_VERSION_FORMS = str.maketrans("镜雙晓曉樱桜辉华堇极將霸", "鏡双暁暁櫻櫻輝華菫極将覇")
_ACHIEVEMENT = re.compile(r"[真超檄橙暁桃櫻紫菫白雪輝熊華爽煌宙星祭祝双宴鏡彩舞](?:極|将|神|舞舞)")


def normalize_plate_name(name: str) -> str:
    trimmed = name.strip()
    candidate = "".join(unicodedata.normalize("NFKC", trimmed).split()).translate(_VERSION_FORMS)
    if candidate in {"舞", "舞舞"}:
        return "舞舞舞"
    if candidate in {"覇", "覇者"}:
        return "覇者"
    return candidate if _ACHIEVEMENT.fullmatch(candidate) else trimmed
