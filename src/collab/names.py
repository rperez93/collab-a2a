"""Reserve the hub label without changing anybody else's Unicode identity.

Only the reserved label is compared; no skeleton is stored or used to resolve
participants. Names remain labels, never identity or system-event authority.
"""
import unicodedata

# Target-only extraction from official Unicode Security Mechanisms data:
# https://www.unicode.org/Public/security/latest/confusables.txt
# Version 18.0.0, dated 2026-08-06; retrieved 2026-11-14.
# Source SHA256: 6ed3ee967c9dfdf6677d563c9985182fbc50a2efb7d6059cd57b2e2ce18f5b92
# Each source character below has an official target which NFKD/casefolds to
# a substring of collab after marks/separators are removed. Case variants use the
# same lookup. This is a reserved-label comparison, not a general UTS39 API.
# ASCII i is additionally folded to l so coIIab/coiiab cannot impersonate the
# all-caps banner after casefolding; 1/0 are covered by official mappings.
_CONFUSABLES = {
    'a': '\u249c\U0001f110\u237a\uff41\U0001d41a\U0001d44e\U0001d482\U0001d4b6\U0001d4ea\U0001d51e\U0001d552\U0001d586\U0001d5ba\U0001d5ee\U0001d622\U0001d656\U0001d68a\U0001df5a\u0251\uab64\u03b1\U0001d6c2\U0001d6fc\U0001d736\U0001d770\U0001d7aa\u0430\uff21\U0001ccd6\U0001d400\U0001d434\U0001d468\U0001d49c\U0001d4d0\U0001d504\U0001d538\U0001d56c\U0001d5a0\U0001d5d4\U0001d608\U0001d63c\U0001d670\U0001df6a\u0391\U0001d6a8\U0001d6e2\U0001d71c\U0001d756\U0001d790\u0410\u13aa\u15c5\ua4ee\U00016f40\U000102a0\u2376\U0001df41\U0001df40\u1e9a',
    'b': '\u147e\u1480\u0181\u249d\U0001f111\U0001d207\uff42\U0001d41b\U0001d44f\U0001d483\U0001d4b7\U0001d4eb\U0001d51f\U0001d553\U0001d587\U0001d5bb\U0001d5ef\U0001d623\U0001d657\U0001d68b\u0184\u042c\u07d5\u13cf\u1472\u15af\U00016eb6\U0001031c\uff22\u212c\U0001ccd7\U0001d401\U0001d435\U0001d469\U0001d4d1\U0001d505\U0001d539\U0001d56d\U0001d5a1\U0001d5d5\U0001d609\U0001d63d\U0001d671\ua7b4\u0392\U0001d6a9\U0001d6e3\U0001d71d\U0001d757\U0001d791\u2c82\u0412\u13f4\u15f7\ua557\ua4d0\U00010282\U000102a1\U00010301\ua69d\u0253\u1473\u0183\u0182\u0411\u0180\u048d\u048c\u0463\u0462\u147f\u1481\u1488',
    'c': '\u249e\U0001f112\U0001d134\uff43\u217d\U0001d41c\U0001d450\U0001d484\U0001d4b8\U0001d4ec\U0001d520\U0001d554\U0001d588\U0001d5bc\U0001d5f0\U0001d624\U0001d658\U0001d68c\u1d04\u03f2\u2ca5\u0441\u1c83\u1004\u105a\uabaf\U0001043d\U0001f74c\U000118e9\U000118f2\uff23\u216d\u2102\u212d\U0001ccd8\U0001d402\U0001d436\U0001d46a\U0001d49e\U0001d4d2\U0001d56e\U0001d5a2\U0001d5d6\U0001d60a\U0001d63e\U0001d672\u03f9\u2ca4\u0421\u13df\ua4da\U000102a2\U00010302\U00010415\U0001051b\xa2\uffe0\u023c\u20a1\U0001f16e\u04ab\u04aa\u0187',
    'co': '\u2105\uab43\uab44\u33c7',
    'l': '\u14b6\u2474\U0001f118\u24a7\U0001f11b\ufe31\u0964\ua8ce\u104a\uaa5d\U00011047\U000110c0\U00011141\U000111c5\U000113d4\U0001144b\U00011641\U00011c41\u05c0\U000115c5\u21bf|\uff5c\u2223\u23fd\u2502\uffe8\u2503\U0001d1001\u0661\u06f1\U00010320\U00010cfa\U00011de1\U0001d377\U0001e141\U0001e8c7\U0001ed01\uff11\U0001ccf1\U0001d7cf\U0001d7d9\U0001d7e3\U0001d7ed\U0001d7f7\U0001fbf1\ua830I\uff29\u2160\u2110\u2111\U0001ccde\U0001d408\U0001d43c\U0001d470\U0001d4d8\U0001d540\U0001d574\U0001d5a8\U0001d5dc\U0001d610\U0001d644\U0001d678\ua7ae\ua7fe\u0196\uff4c\u217c\u2113\U0001d425\U0001d459\U0001d48d\U0001d4c1\U0001d4f5\U0001d529\U0001d55d\U0001d591\U0001d5c5\U0001d5f9\U0001d62d\U0001d661\U0001d695\ua781\u01c0\u0399\U0001d6b0\U0001d6ea\U0001d724\U0001d75e\U0001d798\u2c92\u0406\u04cf\u04c0\u05d5\u05df\u0627\U0001ee00\U0001ee80\ufe8e\ufe8d\u07ca\u2d4a\u2d4f\U00011dda\u16c1\u16d0\U00010ca5\U00010c3e\ua56f\U00016eaa\ua4f2\U00016f28\U0001028a\U00010926\U00010309\U0001050e\U00016d63\U0001d22a\uff2c\u216c\u2112\U0001cce1\U0001d40b\U0001d43f\U0001d473\U0001d4db\U0001d50f\U0001d543\U0001d577\U0001d5ab\U0001d5df\U0001d613\U0001d647\U0001d67b\u2cd0\u13de\u14aa\ua4e1\U00016f16\U000118a3\U000118b2\U0001041b\U00010526\ufd3c\ufd3d\ufe82\ufe81\u0142\u16c5\u0141\u026d\u0197\U0001df6e\u019a\U00016ae4\u026b\ufe88\ufe87\u0673\u0140\u013f\u14b7\U0001f102\u2488\u05f1\u215f',
    'll': '\u247e\u0965\ua8cf\u104b\U00011048\U000110c1\U00011142\U000111c6\U000113d5\U0001144c\U00011642\U00011c42\u2016\u2225\U0001d262\u2161\u01c1\u05f0\u1efa\U00010199\u2492',
    'o': '\u24aa\u3207\U0001f11e\u0c02\u0c82\u0d02\u0d82\U00011302\u0966\u09e6\u0a66\u0ae6\u0b66\u0be6\u0c66\u0d66\u0e50\u0ed0\u1040\u17e0\u1a80\u1a90\U000114d0\u0665\u06f5\uff4f\u2134\U0001d428\U0001d45c\U0001d490\U0001d4f8\U0001d52c\U0001d560\U0001d594\U0001d5c8\U0001d5fc\U0001d630\U0001d664\U0001d698\u1d0f\u1d11\uab3d\u03bf\U0001d6d0\U0001d70a\U0001d744\U0001d77e\U0001d7b8\u03c3\U0001d6d4\U0001d70e\U0001d748\U0001d782\U0001d7bc\u2c9f\u03ed\u043e\u1c82\u10ff\u0585\u05e1\u0647\U0001ee24\U0001ee84\ufeeb\ufeec\ufeea\ufee9\u06be\ufbac\ufbad\ufbab\ufbaa\u06c1\ufba8\ufba9\ufba7\ufba6\u06d5\u0840\u07cb\u0d20\u1bea\u101d\U00010d07\U00011124\u1a45\U000104ea\u110b\u3147\uffb7\u11bc\U000118c8\U000118d7\U0001092c\U0001042c0\u07c0\u0ce6\u3007\U000118e0\U00011de0\U0001e140\U0001e2f0\uff10\U0001ccf0\U0001d7ce\U0001d7d8\U0001d7e2\U0001d7ec\U0001d7f6\U0001fbf0\uff2f\U0001cce4\U0001d40e\U0001d442\U0001d476\U0001d4aa\U0001d4de\U0001d512\U0001d546\U0001d57a\U0001d5ae\U0001d5e2\U0001d616\U0001d64a\U0001d67e\u039f\U0001d6b6\U0001d6f0\U0001d72a\U0001d764\U0001d79e\u2c9e\u041e\u1cbf\u0555\u2d54\u12d0\u0b20\U000104c2\U00010c17\U00016ae9\ua4f3\U000118b5\U00010292\U000102ab\U0001030f\U00010404\U00010516\u06ff\u0629\ufe94\ufe93\u06c3\u2365\xf8\uab3e\u2205\u2300\xd8\u2d41\ua6a2\u0275\ua74b\u2c91\u04e9\u0473\uab8e\uabbb\u2296\u229d\u236c\U0001d21a\U0001f714\U00016e80\u019f\ua74a\u03b8\u03d1\U0001d6c9\U0001d6dd\U0001d703\U0001d717\U0001d73d\U0001d751\U0001d777\U0001d78b\U0001d7b1\U0001d7c5\u0398\u03f4\U0001d6af\U0001d6b9\U0001d6e9\U0001d6f3\U0001d723\U0001d72d\U0001d75d\U0001d767\U0001d797\U0001d7a1\u2c90\u04e8\u0472\u2d31\u1340\u13be\u13eb\U0001699b\U0001051e\U0001df3c\uab74\u13a4\ufba5\ufba4\ufcd9\U0001f101\U0001f100\u10d7\u1010\U00011116\u1a32',
    'ol': '\U0001ee64',
}

_LOOKALIKE = {char: target for target, chars in _CONFUSABLES.items() for char in chars}


def is_reserved_participant_name(name: str) -> bool:
    """True only for the hub label and normalized/confusable spellings of it.

    Strip decorations/combining and format characters only for this comparison.
    Other Unicode names are neither rejected nor rewritten by this policy.
    """
    parts = []
    for char in name:
        mapped = _LOOKALIKE.get(char, _LOOKALIKE.get(char.upper(), char))
        for normal in unicodedata.normalize("NFKD", mapped):
            if unicodedata.category(normal)[0] not in "MZPC":
                parts.append(normal.casefold().replace("i", "l"))
    return "".join(parts) == "collab"


def validate_participant_name(name: str) -> None:
    """Refuse the reserved label before any participant insertion or rename."""
    if is_reserved_participant_name(name):
        raise ValueError("the name collab and its lookalikes are reserved for hub notices")

# Derived Unicode data license follows.
# UNICODE LICENSE V3
#
# COPYRIGHT AND PERMISSION NOTICE
#
# Copyright © 1991-2026 Unicode, Inc.
#
# NOTICE TO USER: Carefully read the following legal agreement. BY
# DOWNLOADING, INSTALLING, COPYING OR OTHERWISE USING DATA FILES, AND/OR
# SOFTWARE, YOU UNEQUIVOCALLY ACCEPT, AND AGREE TO BE BOUND BY, ALL OF THE
# TERMS AND CONDITIONS OF THIS AGREEMENT. IF YOU DO NOT AGREE, DO NOT
# DOWNLOAD, INSTALL, COPY, DISTRIBUTE OR USE THE DATA FILES OR SOFTWARE.
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of data files and any associated documentation (the "Data Files") or
# software and any associated documentation (the "Software") to deal in the
# Data Files or Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, and/or sell
# copies of the Data Files or Software, and to permit persons to whom the
# Data Files or Software are furnished to do so, provided that either (a)
# this copyright and permission notice appear with all copies of the Data
# Files or Software, or (b) this copyright and permission notice appear in
# associated Documentation.
#
# THE DATA FILES AND SOFTWARE ARE PROVIDED "AS IS", WITHOUT WARRANTY OF ANY
# KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT OF
# THIRD PARTY RIGHTS.
#
# IN NO EVENT SHALL THE COPYRIGHT HOLDER OR HOLDERS INCLUDED IN THIS NOTICE
# BE LIABLE FOR ANY CLAIM, OR ANY SPECIAL INDIRECT OR CONSEQUENTIAL DAMAGES,
# OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS,
# WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION,
# ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THE DATA
# FILES OR SOFTWARE.
#
# Except as contained in this notice, the name of a copyright holder shall
# not be used in advertising or otherwise to promote the sale, use or other
# dealings in these Data Files or Software without prior written
# authorization of the copyright holder.
