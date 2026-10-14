/* Output-only HUD. Runtime labels are constructed here, never supplied by the
 * controller. This does not certify base-game fidelity or a passing receipt. */
#include "shim.hpp"

extern "C" {

static int hud_enabled, hud_required;
int g_precision_tick;   /* 0: "FRAME" (the default precision), 1: "TICK" */
static i64 hud_offset;
static int hud_stale;
/* Original 5x7 column glyphs, least significant bit at the top. */
static constexpr u8 digits[10][5]={
 {62,81,73,69,62},{0,66,127,64,0},{98,81,73,73,70},{34,65,73,73,54},
 {24,20,18,127,16},{39,69,69,69,57},{60,74,73,73,48},{1,113,9,5,3},
 {54,73,73,73,54},{6,73,73,41,30}};
static constexpr u8 letters[26][5]={
 {126,9,9,9,126},{127,73,73,73,54},{62,65,65,65,34},{127,65,65,34,28},
 {127,73,73,73,65},{127,9,9,9,1},{62,65,73,73,122},{127,8,8,8,127},
 {0,65,127,65,0},{32,64,65,63,1},{127,8,20,34,65},{127,64,64,64,64},
 {127,2,12,2,127},{127,4,8,16,127},{62,65,65,65,62},{127,9,9,9,6},
 {62,65,81,33,94},{127,9,25,41,70},{38,73,73,73,50},{1,1,127,1,1},
 {63,64,64,64,63},{31,32,64,32,31},{63,64,56,64,63},{99,20,8,20,99},
 {3,4,120,4,3},{97,81,73,69,67}};
static u8 glyph(char c,u32 x){
 if(c>='a'&&c<='z')c-=32;
 if(c>='A'&&c<='Z')return letters[c-'A'][x];
 if(c>='0'&&c<='9')return digits[c-'0'][x];
 if(c=='-')return 8;if(c==':')return x==2?36:0;
 if(c=='.')return x==2?64:0;if(c=='/')return (u8)(64u>>x);
 if(c==' ')return 0;return x==0||x==4?127:65;
}
/* Compact 3x5 face for the status chip ("TAS PoC by Kolto Labs"). Same column-major layout. */
static const u8 small_glyphs[36][3]={{30,5,30},{31,21,10},{14,17,17},{31,17,14},{31,21,17},{31,5,1},{14,17,29},{31,4,31},{17,31,17},{8,16,15},{31,4,27},{31,16,16},{31,6,31},{31,1,30},{14,17,14},{31,5,2},{14,25,22},{31,5,26},{18,21,9},{1,31,1},{31,16,31},{15,16,15},{31,12,31},{27,4,27},{3,28,3},{25,21,19},{31,17,31},{18,31,16},{25,21,18},{17,21,10},{7,4,31},{23,21,9},{30,21,29},{1,29,3},{31,21,31},{23,21,15}};
static u8 glyph_small(char c,u32 x){
 if(c>='a'&&c<='z')c-=32;
 if(c>='A'&&c<='Z')return small_glyphs[c-'A'][x];
 if(c>='0'&&c<='9')return small_glyphs[26+c-'0'][x];
 if(c=='-')return x<3?4:0;if(c==':')return x==1?10:0;if(c=='.')return x==1?16:0;
 return 0;
}
void hud_lock(int tas){hud_required=tas;}
int hud_config(int on,int watermark,i64 offset,const char *text){
 if(hud_required&&!watermark)return 0;
 hud_enabled=on;hud_offset=offset;
 /* Legacy controller prose is accepted for protocol compatibility. The native
  * surface shows runtime status only; the workspace owns controls and inputs. */
 (void)text;return 1;
}
int hud_on(void){return hud_enabled||hud_required;}
static void hud_label(u64 tick,char text[160]){
 buf_t b;b_init(&b,text,160);b_str(&b,hud_required?"TAS PoC by Kolto Labs":"TAS PoC by Kolto Labs  LAB");
 if(hud_enabled){
  b_str(&b,g_precision_tick?"  TICK ":"  FRAME ");b_i64(&b,(i64)tick+hud_offset);
  if(hud_stale)b_str(&b,"  VIEW PENDING");
 }
 b_term(&b);
}
#include "hud_badge.inc"

/* One content-sized status chip. The glyph badge is secondary to the readable
 * TAS label. No key states or controller-supplied shortcut/debug prose. */
static u32 hud_panel_width(u64 tick){
 char text[160];hud_label(tick,text);
 return 12u+(u32)strlen(text)*4u+(hud_required?HUD_BADGE_W+12u:0u);
}
#define HUD_PANEL_HEIGHT 18u
/* BGRA bottom-up. Only the chip is repainted on captured game frames. */
void hud_pixels(u8 *pixels,u32 w,u32 h,u64 tick){
 if(!hud_on()||!pixels||!w||!h)return;
 char text[160];hud_label(tick,text);
 u32 want=hud_panel_width(tick),pw=w<want?w:want,ph=h<HUD_PANEL_HEIGHT?h:HUD_PANEL_HEIGHT;
 for(u32 y=0;y<ph;y++)for(u32 x=0;x<pw;x++){
  u8 *p=pixels+((u64)(h-1-y)*w+x)*4;p[0]=30;p[1]=27;p[2]=24;p[3]=255;
 }
 for(u32 c=0;text[c];c++){
  u32 left=6+c*4,top=6;if(left+3>pw)break;
  for(u32 x=0;x<3;x++){u8 bits=glyph_small(text[c],x);
   for(u32 y=0;y<7;y++)if(bits&(1u<<y)){
    u32 py=top+y,px=left+x;if(py>=ph)continue;
    u8 *p=pixels+((u64)(h-1-py)*w+px)*4;p[0]=219;p[1]=169;p[2]=57;p[3]=255;
   }
  }
 }
 if(hud_required&&pw>=want)for(u32 y=0;y<HUD_BADGE_H;y++)for(u32 x=0;x<HUD_BADGE_W;x++){
  u32 py=2+y,px=pw-HUD_BADGE_W-6+x;if(py>=ph)continue;
  u32 alpha=hud_badge[y*HUD_BADGE_W+x];u8 *p=pixels+((u64)(h-1-py)*w+px)*4;
  p[0]=(u8)((30*(255-alpha)+150*alpha)/255);
  p[1]=(u8)((27*(255-alpha)+220*alpha)/255);
  p[2]=(u8)((24*(255-alpha)+245*alpha)/255);
 }
}

/* A separate layered presentation surface: never draw into, read, or change
 * the game's GL framebuffer/context. No activation, input or new thread.
 * Focus/message neutrality remains an actual-game acceptance obligation. */
static HWND hud_window,hud_owner;
static HDC hud_dc;
static HBITMAP hud_bitmap;
static u8 *hud_dib;
static u32 hud_width,hud_height;
static int hud_class;
static u64 hud_draws,hud_skips;
static LRESULT CALLBACK hud_proc(HWND w,UINT msg,WPARAM wp,LPARAM lp){
 if(msg==WM_NCHITTEST)return HTTRANSPARENT;
 if(msg==WM_MOUSEACTIVATE)return MA_NOACTIVATE;
 if(msg==WM_ERASEBKGND)return 1;
 return DefWindowProcA(w,msg,wp,lp);
}
static void hud_release_surface(void){
 if(hud_window&&IsWindow(hud_window)&&
    (WNDPROC)GetWindowLongPtrA(hud_window,GWLP_WNDPROC)==hud_proc)DestroyWindow(hud_window);
 if(hud_dc)DeleteDC(hud_dc);
 if(hud_bitmap)DeleteObject(hud_bitmap);
 hud_window=NULL;hud_owner=NULL;hud_dc=NULL;hud_bitmap=NULL;hud_dib=NULL;hud_width=hud_height=0;
}
static int hud_surface(HWND owner,u32 w,u32 h){
 DWORD pid=0;GetWindowThreadProcessId(owner,&pid);
 if(pid!=GetCurrentProcessId())return 0;
 if(hud_window&&(!IsWindow(hud_window)||hud_owner!=owner))hud_release_surface();
 if(!hud_window){
  WNDCLASSA cls;memset(&cls,0,sizeof cls);cls.lpfnWndProc=hud_proc;
  cls.hInstance=GetModuleHandleA(NULL);cls.lpszClassName="KPTAS.OutputHUD.v1";
  if(!hud_class&&!RegisterClassA(&cls))return 0;
  hud_class=1;
  hud_window=CreateWindowExA(WS_EX_LAYERED|WS_EX_TRANSPARENT|WS_EX_TOOLWINDOW|WS_EX_NOACTIVATE,
      cls.lpszClassName,"Stasis HUD",WS_POPUP,0,0,(int)w,(int)h,
      owner,NULL,cls.hInstance,NULL);
  if(!hud_window)return 0;
  hud_owner=owner;
 }
 if(!hud_dc){hud_dc=CreateCompatibleDC(NULL);if(!hud_dc)return 0;}
 if(hud_owner!=owner)return 0;
 if(w!=hud_width||h!=hud_height){
  BITMAPINFO info;memset(&info,0,sizeof info);info.bmiHeader.biSize=sizeof info.bmiHeader;
  info.bmiHeader.biWidth=(LONG)w;info.bmiHeader.biHeight=(LONG)h;
  info.bmiHeader.biPlanes=1;info.bmiHeader.biBitCount=32;info.bmiHeader.biCompression=BI_RGB;
  void *bits=NULL;HBITMAP next=CreateDIBSection(hud_dc,&info,DIB_RGB_COLORS,&bits,NULL,0);
  if(!next||!bits)return 0;
  HGDIOBJ previous=SelectObject(hud_dc,next);
  if(!previous||previous==HGDI_ERROR){DeleteObject(next);return 0;}
  if(hud_bitmap)DeleteObject(previous);
  hud_bitmap=next;hud_dib=static_cast<u8*>(bits);hud_width=w;hud_height=h;
 }
 return 1;
}
static void hud_draw_host(void *arg){
 DWORD error=GetLastError();math_host_scope_t scope;math_scope_begin(&scope,MATH_HOST_FP|MATH_HOST_ID);
 HWND owner=(HWND)arg;RECT r;POINT point={0,0};
 /* Restore can present through the raw driver before our first SwapBuffers
  * hook. Resolve the current real drawable rather than requiring a cached HUD. */
 if(!owner){
  using current_dc_fn = HDC (WINAPI *)(void);
  current_dc_fn current_dc=reinterpret_cast<current_dc_fn>(GetProcAddress(GetModuleHandleA("opengl32.dll"),"wglGetCurrentDC"));
  HDC dc=current_dc?current_dc():NULL;
  owner=dc?WindowFromDC(dc):hud_owner;
 }
 {
 if(!hud_on()||!owner||IsIconic(owner)||GetForegroundWindow()!=owner){
  /* Hide once: a hidden HUD costs no window call per frame. */
  if(hud_window&&IsWindowVisible(hud_window))ShowWindow(hud_window,SW_HIDE);goto done;
 }
 if(!GetClientRect(owner,&r)||!ClientToScreen(owner,&point)||r.right<=0||r.bottom<=0){hud_skips++;goto done;}
 u32 want=hud_panel_width(g_gate.tick);
 u32 w=(u32)r.right<want?(u32)r.right:want;
 u32 h=(u32)r.bottom<HUD_PANEL_HEIGHT?(u32)r.bottom:HUD_PANEL_HEIGHT;
 if(!hud_surface(owner,w,h)){hud_skips++;goto done;}
 hud_pixels(hud_dib,w,h,g_gate.tick);
 SIZE size={(LONG)w,(LONG)h};POINT source={0,0};BLENDFUNCTION blend={AC_SRC_OVER,0,255,AC_SRC_ALPHA};
 if(!UpdateLayeredWindow(hud_window,NULL,&point,&size,hud_dc,&source,0,&blend,ULW_ALPHA)){
  hud_skips++;goto done;
 }
 SetWindowPos(hud_window,NULL,point.x,point.y,(int)w,(int)h,
              SWP_NOACTIVATE|SWP_NOZORDER|SWP_NOOWNERZORDER|SWP_SHOWWINDOW);
 hud_draws++;
 }
done:
 math_scope_end(&scope);SetLastError(error);
}
static void hud_swap_host(void *arg){
 /* Output only: the HUD is drawn only with a real present. A frame render
  * skipping leaves unpresented (rule above wire_render_due) keeps the HUD
  * and hud_stale as they were. */
 if(!render_present_due())return;
 DWORD error=GetLastError();math_host_scope_t scope;math_scope_begin(&scope,MATH_HOST_FP|MATH_HOST_ID);
 hud_stale=0;hud_draw_host(WindowFromDC((HDC)arg));
 math_scope_end(&scope);SetLastError(error);
}
void hud_swap(HDC dc){
 DWORD error=GetLastError();kst_hcall(hud_swap_host,(void*)dc);SetLastError(error);
}
void hud_refresh(void){hud_draw_host(NULL);}
void hud_restored(void){
 DWORD error=GetLastError();math_host_scope_t scope;math_scope_begin(&scope,MATH_HOST_FP|MATH_HOST_ID);
 hud_stale=1;
 /* Hide before the verified raw presentation. Drawing a wider pending chip
  * then shrinking it leaves stale Wine layered-window pixels on the drawable.
  * Captured invalid views retain their pending label; the live surface returns
  * only after exact presentation succeeds (or a real game swap refreshes it). */
 if(hud_window&&IsWindow(hud_window)&&
    (WNDPROC)GetWindowLongPtrA(hud_window,GWLP_WNDPROC)==hud_proc)ShowWindow(hud_window,SW_HIDE);
 math_scope_end(&scope);SetLastError(error);
}
/* Called only after the restored drawable was physically presented and all
 * four framebuffer planes read back exactly. No hook swap or tick advance. */
void hud_verified_frame(void){hud_stale=0;hud_refresh();}

/* One host-only dispatcher. Event recognition runs before physical capture;
 * polling only drains this queue. Restoring game memory cannot rewind keys. */
static constexpr const char *tool_actions[]={"stop","branch","rewind_mark","rewind","step",
 "play_pause","toggle_editor","save","apply","switch_layout","reload_ui",
 "toggle_engagement","toggle_visibility"};
static hud_binding_t tool_bindings[16];
static u32 tool_count;
static u8 tool_down[256],tool_quarantine[256],tool_queue[32];
static u32 tool_head,tool_used;
static int tool_configured,tool_overflow,tool_focused;
static u32 tool_modifiers(void){
 return ((tool_down[VK_CONTROL]||tool_down[VK_LCONTROL]||tool_down[VK_RCONTROL])?1u:0u)|
        ((tool_down[VK_MENU]||tool_down[VK_LMENU]||tool_down[VK_RMENU])?2u:0u)|
        ((tool_down[VK_SHIFT]||tool_down[VK_LSHIFT]||tool_down[VK_RSHIFT])?4u:0u)|
        ((tool_down[VK_LWIN]||tool_down[VK_RWIN])?8u:0u);
}
int hud_bindings_configure(const hud_binding_t *bindings,u32 count){
 if(count>16)return 0;
 for(u32 i=0;i<count;i++){
  if(bindings[i].action>=13||!bindings[i].vk||bindings[i].vk>255||bindings[i].modifiers>15)return 0;
  for(u32 j=0;j<i;j++)if(bindings[i].vk==bindings[j].vk&&bindings[i].modifiers==bindings[j].modifiers)return 0;
 }
 memcpy(tool_bindings,bindings,count*sizeof *bindings);tool_count=count;
 tool_head=tool_used=0;tool_overflow=0;tool_configured=1;
 for(u32 i=0;i<256;i++)tool_down[i]=tool_quarantine[i]=(GetAsyncKeyState((int)i)&0x8000)!=0;
 return 1;
}
static void tool_defaults(void){
 if(tool_configured)return;
 // Keep native fallback chords aligned with DEFAULT_SHORTCUTS in
 // kptas.telos.ui_preferences. Older Ctrl+Alt+Fn transport chords remain
 // available for the actions that have no shared preference default; they
 // must not steal Ctrl+Shift+F11, which means Engage in the current UI.
 const hud_binding_t defaults[]={
  {VK_F11,3,0}, {VK_F10,3,1}, {VK_F9,7,2},
  {VK_F9,1,3}, {VK_F8,1,4}, {VK_F7,1,5}, {VK_F6,1,6},
  {'S',3,7}, {VK_RETURN,3,8}, {'L',3,9}, {'R',3,10},
  {VK_F11,5,11}, {VK_F12,5,12}
 };
 hud_bindings_configure(defaults,sizeof defaults/sizeof defaults[0]);
}
int hud_control_observe(const MSG *message,u8 *suppressed,u32 *count){
 *count=0;tool_defaults();
 HWND game=(HWND)wh_real(*(HWND *)(uintptr_t)ADDR_RENDER_WINDOW);
 int focused=game&&wh_real(GetForegroundWindow())==game;
 if(focused!=tool_focused){
  tool_focused=focused;
  for(u32 i=0;i<256;i++)tool_down[i]=tool_quarantine[i]=(GetAsyncKeyState((int)i)&0x8000)!=0;
 }
 if(!message)return 0;
 UINT kind=message->message;
 if(kind!=WM_KEYDOWN&&kind!=WM_SYSKEYDOWN&&kind!=WM_KEYUP&&kind!=WM_SYSKEYUP)return 0;
 u32 vk=(u32)message->wParam;if(!vk||vk>255)return 0;
 bool down=kind==WM_KEYDOWN||kind==WM_SYSKEYDOWN;
 bool previous=tool_down[vk]!=0;tool_down[vk]=down;
 // Wine key messages commonly use generic modifier VKs, whereas async state
 // reports both generic and sided VKs. Keep both views coherent on release.
 u32 sided=0;
 if(vk==VK_CONTROL)sided=(message->lParam&(1u<<24))?VK_RCONTROL:VK_LCONTROL;
 else if(vk==VK_MENU)sided=(message->lParam&(1u<<24))?VK_RMENU:VK_LMENU;
 else if(vk==VK_SHIFT)sided=(((u32)message->lParam>>16)&0xff)==0x36?VK_RSHIFT:VK_LSHIFT;
 if(sided){tool_down[sided]=down;if(!down)tool_quarantine[sided]=0;}

 if(tool_quarantine[vk]){if(!down)tool_quarantine[vk]=0;return 0;}
 if(!focused||message->hwnd!=game)return 0;
 if(!down||previous||(message->lParam&(1u<<30)))return 0;
 u32 modifiers=tool_modifiers();
 for(u32 i=0;i<tool_count;i++)if(tool_bindings[i].vk==vk&&tool_bindings[i].modifiers==modifiers){
  if(tool_used==32)tool_overflow=1;
  else{tool_queue[(tool_head+tool_used)%32]=(u8)tool_bindings[i].action;tool_used++;}
  u8 scan=(u8)(((u32)message->lParam>>16)&0x7f);
  if(scan)suppressed[(*count)++]=(u8)(scan|((message->lParam&(1u<<24))?0x80:0));
  if(modifiers&1){suppressed[(*count)++]=0x1d;suppressed[(*count)++]=0x9d;}
  if(modifiers&2){suppressed[(*count)++]=0x38;suppressed[(*count)++]=0xb8;}
  if(modifiers&4){suppressed[(*count)++]=0x2a;suppressed[(*count)++]=0x36;}
  if(modifiers&8){suppressed[(*count)++]=0xdb;suppressed[(*count)++]=0xdc;}
  return 1;
 }
 return 0;
}
const char *hud_control_poll(void){
 DWORD error=GetLastError();math_host_scope_t scope;math_scope_begin(&scope,MATH_HOST_FP|MATH_HOST_ID);
 msg_host_events();
 const char *action="";
 if(tool_overflow){tool_overflow=0;tool_head=tool_used=0;action="stop";}
 else if(tool_used){action=tool_actions[tool_queue[tool_head]];tool_head=(tool_head+1)%32;tool_used--;}
 math_scope_end(&scope);SetLastError(error);return action;
}

void hud_stats_json(buf_t *b){
 b_str(b,"{\"requested\":");b_str(b,hud_on()?"true":"false");
 b_str(b,",\"draws\":");b_u64(b,hud_draws);b_str(b,",\"skips\":");b_u64(b,hud_skips);
 b_str(b,",\"host_input_dropped\":");b_u64(b,g_msg_host_dropped);
 b_str(b,",\"fidelity\":\"unverified\"}");
}

} // extern "C"
