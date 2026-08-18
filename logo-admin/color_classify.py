"""Keyword-based garment-color light/dark classifier.

Returns ('light'|'dark', confidence 0-1). 'dark' garments take a WHITE logo,
'light' garments take a BLACK logo. Bright/hi-viz garments show a black logo,
so they classify as 'light'. Best-effort baseline for human review; there is
no clean historical signal (lb-black/lb-white is selector CSS, not the garment).
"""
import re

BRIGHT = ["hi-viz","hiviz","hi viz","high viz","safety","neon","blaze","fluorescent","fluor",
          "volt","electric","bright","optic","glow"]
LIGHT = ["white","natural","cream","ivory","khaki","tan","stone","sand","beige","light","pale",
         "sky","mint","pastel","yellow","gold","lime","snow","oatmeal","wheat","vanilla","pearl",
         "platinum","chrome","aluminum","dove","fog","mist","cloud","linen","bone","eggshell",
         "banana","lemon","canary","marigold","sunflower","champagne","blush","peach","apricot",
         "rose","pink","lavender","lilac","powder","baby","seafoam","celadon","celery","honeydew",
         "aqua","carolina","columbia","robin","birch","putty","biscuit","almond","buff","camel",
         "fawn","straw","flax","parchment","chalk","frost","glacier","arctic","alpine","seashell",
         "shell","dune","desert","stucco","greige","mushroom","butter","citron","daffodil",
         "goldenrod","corn","maize","cornsilk","ecru","tofu","orange","tangerine","coral","salmon",
         "melon","cantaloupe","papaya","mango","carrot","amber"]
DARK = ["black","navy","charcoal","forest","hunter","olive","brown","chocolate","espresso","coffee",
        "maroon","burgundy","wine","merlot","garnet","cabernet","cranberry","cardinal","brick",
        "oxblood","purple","plum","eggplant","aubergine","indigo","slate","graphite","gunmetal",
        "gunpowder","anthracite","midnight","onyx","jet","coal","ebony","blackout","blacktop",
        "evergreen","bottle","teal","royal","deep","dark","pine","spruce","moss","army","military",
        "loden","mahogany","walnut","chestnut","cocoa","mocha","java","raisin","blackberry",
        "obsidian","truffle","bison","cypress","juniper","peat","steel","battleship","denim",
        "dungaree","gorge","clover","shamrock","emerald","carbon","asphalt","pewter","storm","raven",
        "bark","umber","sienna","aegean","admiral","adriatic","cobalt","sapphire","marine","peacock",
        "petrol","dusk","twilight","bronze","rust","chili","currant","port","sangria","iron","diesel"]
GREY_DARK = ["charcoal","steel","dark","graphite","slate","gunmetal","carbon","storm","iron",
             "battleship","pewter","anthracite","diesel","gunpowder"]
GREY_LIGHT = ["heather","ash","light","silver","pearl","dove","cool","fog","mist","platinum","frost",
              "cloud","shady","fossil","greige","athletic","oxford","sport","marle","marl","alloy",
              "cement","concrete","nickel","sterling","quarry"]

def _has(name, words):
    return any(re.search(r'(?<![a-z])' + re.escape(w) + r'(?![a-z])', name) for w in words)

def classify_color(name):
    n = (name or "").lower().strip()
    if _has(n, BRIGHT):
        return "light", 0.9
    is_grey = re.search(r'(?<![a-z])(grey|gray)(?![a-z])', n)
    dark = _has(n, DARK)
    light = _has(n, LIGHT)
    if is_grey:
        gd, gl = _has(n, GREY_DARK), _has(n, GREY_LIGHT)
        if gd and not gl: return "dark", 0.88
        if gl and not gd: return "light", 0.85
        if dark and not light: return "dark", 0.7
        if light and not dark: return "light", 0.7
        return "dark", 0.5
    if dark and not light: return "dark", 0.85
    if light and not dark: return "light", 0.85
    if dark and light:
        first = re.split(r'[\s/,-]+', n)[0]
        if _has(first, DARK): return "dark", 0.55
        if _has(first, LIGHT) or _has(first, BRIGHT): return "light", 0.55
        return "dark", 0.45
    return "dark", 0.4
