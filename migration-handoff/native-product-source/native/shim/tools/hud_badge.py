"""Generate the HUD's TAS badge from the pinned MIT Aurebesh Rodian font.
Run with Python + Pillow; the native build consumes the checked-in bitmap.
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
root=Path(__file__).resolve().parents[1]
font=ImageFont.truetype(str(root/'assets/aurebesh-rodian/Aurebesh_Rodian_beta.otf'),24)
box=font.getbbox('tas');w,h=box[2]-box[0],box[3]-box[1]
im=Image.new('L',(w,h));ImageDraw.Draw(im).text((-box[0],-box[1]),'tas',font=font,fill=255)
rows=['/* Derived from Aurebesh Rodian, copyright2020 AurekFonts, MIT. See assets/aurebesh-rodian/LICENSE. */',f'#define HUD_BADGE_W {w}u',f'#define HUD_BADGE_H {h}u','static const u8 hud_badge[]={']
data=list(im.tobytes())
rows += [','.join(str(v) for v in data[i:i+24])+',' for i in range(0,len(data),24)]
rows+=['};','']
(root/'src/hud_badge.inc').write_text('\n'.join(rows))
print(w,h)
