/* Copyright 2026 André S. Gomes
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 *     Unless required by applicable law or agreed to in writing, software
 *     distributed under the License is distributed on an "AS IS" BASIS,
 *     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 *     See the License for the specific language governing permissions and
 *     limitations under the License.
 */

/*
 * Sat Track — overlay app. Doppler tracking for FM satellites without a clock:
 * tools/sattrack/sattrack.py stores each pass as range rate samples from AOS
 * (sat_format.h), MENU starts the pass at AOS and ticks_ms() plays it back.
 * Downlink and uplink are both corrected from the same range rate.
 *
 * Keys: MENU start at AOS / next pass · UP/DOWN pick a pass, or move the pass
 *       1 s during it · 0 mark a pass flown (again: clear), or stop/resume it
 *       3/9 tune RX up/down · 5 resync (tuning -> timing) · F+UP/DOWN squelch
 *       PTT talk on the corrected uplink · EXIT quit (a running pass resumes).
 */

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "../app_api.h"
#include "sat_format.h"

#define TICK_MS   20
#define REPEAT_MS 300
#define CFG_MAGIC 0x5A
#define NO_PASS   0xFF
#define TRIM_MAX  2000          /* 10 Hz units */
#define RESYNC_MS 60000u
#define FREQ_MIN  1800000u      /* 18 MHz, 10 Hz units */

enum { ST_EMPTY, ST_ARMED, ST_RUN, ST_DONE };

typedef struct {
    uint8_t  magic, run;        /* run: pass left running, or NO_PASS */
    uint16_t used;              /* flown passes of this generation */
    uint32_t generation, t0, exitMs;
} cfg_t;
_Static_assert(sizeof(cfg_t) == 16u, "app config is 16 bytes");

struct globals {
    bool     txDenied, fkey, fheld, stopped, k1;
    uint8_t  st, passNo, sql, pk;   /* pk: peak-hold, meter pixels */
    int16_t  trim, rr;
    uint16_t used, az, el;
    int32_t  tMs;
    uint32_t t0, cacheIdx, tuned, rx, tx;
    const app_api_t *api;
    sat_index_t  ix;
    sat_pass_t   P;
    sat_sample_t cs[2];
    char     text[20];
};
static struct globals g;
#define A (g.api)

/* C division goes to the resident helpers, not libgcc. */
__attribute__((used)) uint64_t __aeabi_uidivmod(uint32_t n,uint32_t d){ return A->uidivmod(n,d); }
__attribute__((used)) uint32_t __aeabi_uidiv(uint32_t n,uint32_t d){ return (uint32_t)A->uidivmod(n,d); }

/* ---- helpers ---- */
static int32_t lerp(int32_t a,int32_t b,uint32_t f,uint32_t d){
    return b>=a ? a+(int32_t)((uint32_t)(b-a)*f/d) : a-(int32_t)((uint32_t)(a-b)*f/d);
}
static uint32_t uabs(int32_t v){ return (uint32_t)(v<0?-v:v); }
/* ---- pass store ---- */
static bool loadIndex(void){
    return A->sat_read(0,&g.ix,sizeof(g.ix)) && g.ix.magic==SAT_INDEX_MAGIC &&
           g.ix.version==SAT_FMT_VERSION && A->crc16(&g.ix,14)==g.ix.crc;
}
/* Continue CRC c over flash: the firmware CRC starts from 0, so c is folded
 * into the first two bytes of each chunk. */
static uint16_t crcFlash(uint16_t c,uint32_t off,uint32_t left){
    uint8_t buf[64];
    while(left){
        uint16_t k=left>sizeof(buf)?(uint16_t)sizeof(buf):(uint16_t)left;
        A->sat_read(off,buf,k);
        buf[0]^=(uint8_t)(c>>8); buf[1]^=(uint8_t)c;
        c=A->crc16(buf,k);
        off+=k; left-=k;
    }
    return c;
}
/* CRC over header (crc = 0), samples, display extras and plot frame. */
static bool readPass(uint8_t n,sat_pass_t *h){
    if(!A->sat_read(SAT_PASS_OFF(n),h,sizeof(*h)) || h->magic!=SAT_PASS_MAGIC ||
       (uint16_t)(h->n_samples-2u)>SAT_MAX_SAMPLES-2u || (uint8_t)(h->step_s-1u)>3u ||
       h->dl_10hz<FREQ_MIN || h->name[sizeof(h->name)-1])
        return false;
    uint16_t crc=h->crc;
    h->crc=0;
    uint16_t c=A->crc16(h,sizeof(*h));
    h->crc=crc;
    c=crcFlash(c,SAT_PASS_OFF(n)+SAT_HDR_SIZE,(uint32_t)h->n_samples*4u);
    c=crcFlash(c,SAT_PASS_OFF(n)+SAT_EXT_OFF,sizeof(sat_ext_t)+SAT_FRAME_W*SAT_FRAME_PAGES);
    return c==crc;
}
static uint8_t findPass(uint8_t from,int8_t dir,bool unflown){
    sat_pass_t h;
    for(uint8_t k=1;k<=SAT_PASS_COUNT;k++){
        uint8_t n=(uint8_t)((from+(dir>0?k:SAT_PASS_COUNT*2u-k))%SAT_PASS_COUNT);
        if(unflown && (g.used&(1u<<n))) continue;
        if(readPass(n,&h)) return n;
    }
    return NO_PASS;
}

/* ---- Doppler ----
 * rx = dl * (1 - rr/c), tx = ul * (1 + rr/c). The host stores k = f * 2^20 / c,
 * which keeps k * |rr| inside 32 bits up to 1.3 GHz. */
static int32_t shift(uint32_t k,int16_t rr){
    uint32_t m=(k*uabs(rr)+(1u<<19))>>20;
    return rr<0?-(int32_t)m:(int32_t)m;
}
static int32_t elapsedMs(void){
    int32_t ms=(int32_t)(A->ticks_ms()-g.t0), ppm=g.ix.clock_ppm;
    uint32_t c=uabs(ms)/1000u*uabs(ppm)/1000u;        /* clock correction */
    return (ms<0)!=(ppm<0) ? ms-(int32_t)c : ms+(int32_t)c;
}
static uint32_t endMs(void){ return (uint32_t)(g.P.n_samples-1u)*g.P.step_s*1000u; }
static void sampleAt(int32_t t){
    uint32_t step=g.P.step_s*1000u, last=g.P.n_samples-1u, i=0, f=0;
    if(t>0){ i=(uint32_t)t/step; f=(uint32_t)t%step; }
    if(i>=last){ i=last-1u; f=step; }
    if(i!=g.cacheIdx){
        A->sat_read(SAT_PASS_OFF(g.passNo)+SAT_HDR_SIZE+i*4u,g.cs,sizeof(g.cs));
        g.cacheIdx=i;
    }
    g.rr=(int16_t)lerp(g.cs[0].rr,g.cs[1].rr,f,step);
    const sat_sample_t *s=&g.cs[f*2u>=step];          /* az/el: nearest sample */
    g.az=(uint16_t)(s->az*360u/256u);
    g.el=(uint16_t)(s->el/2u);
}
static void compute(void){
    if(g.st==ST_DONE){
        sampleAt((int32_t)endMs());
        g.rx=g.P.dl_10hz; g.tx=g.P.ul_10hz;
        return;
    }
    g.tMs=g.st==ST_RUN?elapsedMs():0;
    sampleAt(g.tMs);
    g.rx=(uint32_t)((int32_t)g.P.dl_10hz-shift(g.P.k_dl,g.rr)+g.trim);
    g.tx=g.P.ul_10hz?(uint32_t)((int32_t)g.P.ul_10hz+shift(g.P.k_ul,g.rr)):0;
    if(g.st==ST_RUN && g.tMs>(int32_t)endMs()){        /* LOS */
        g.st=ST_DONE;
        g.used|=(uint16_t)(1u<<g.passNo);
        g.trim=0;
        g.rx=g.P.dl_10hz; g.tx=g.P.ul_10hz;
        A->play_tone(880,200);
    }
}
/* Retune only past 300 Hz (FM) or 30 Hz (SSB): each retune relocks the PLL. */
static void retune(bool force){
    if(g.st==ST_EMPTY) return;
    uint32_t d=g.rx>g.tuned?g.rx-g.tuned:g.tuned-g.rx;
    if(force || d>=(g.P.modulation?3u:30u)){ A->sat_rx(g.rx); g.tuned=g.rx; }
}
static void loadPass(uint8_t n,uint8_t state){
    g.st=ST_EMPTY;
    if(n==NO_PASS || !readPass(n,&g.P)) return;
    g.passNo=n; g.st=state; g.trim=0; g.cacheIdx=0xFFFFFFFFu; g.txDenied=false; g.stopped=false;
    g.pk=0;
    compute();
    A->sat_tune(g.rx,g.tx,g.P.ctcss_01hz,g.P.modulation);
    g.tuned=g.rx;
}
/* Turn the RX tuning into a time shift. Doppler only moves one way during a
 * pass, so the tuned frequency matches one instant. Refused near AOS/LOS where
 * the curve is flat, or beyond 60 s. */
static bool resync(void){
    if(g.st!=ST_RUN || !g.trim) return false;
    int32_t now=elapsedMs();
    sampleAt(now);
    int32_t r0=g.rr, prevT=now, prevR=r0;
    uint32_t mag=uabs(g.trim)*g.P.q8_dl>>8;
    int32_t target=g.trim>0?r0-(int32_t)mag:r0+(int32_t)mag;
    bool fwd=target>r0;
    for(uint32_t dt=250;dt<=RESYNC_MS;dt+=250){
        int32_t t=fwd?now+(int32_t)dt:now-(int32_t)dt;
        if(t<0) break;
        sampleAt(t);
        int32_t r=g.rr;
        if(fwd?r>=target:r<=target){
            uint32_t span=uabs(r-prevR), part=uabs(target-prevR);
            uint32_t move=uabs(prevT-now)+(span?250u*part/span:250u);
            if(fwd) g.t0-=move; else g.t0+=move;
            g.trim=0;
            return true;
        }
        prevT=t; prevR=r;
    }
    return false;
}

/* ---- config ---- */
static void loadConfig(void){
    cfg_t c;
    A->cfg_load((uint8_t *)&c,sizeof(c));
    if(c.magic!=CFG_MAGIC || c.generation!=g.ix.generation) return;
    g.used=c.used;
    /* resume a running pass, unless the radio rebooted (ticks restarted) */
    if(c.run>=SAT_PASS_COUNT || (int32_t)(A->ticks_ms()-c.exitMs)<0) return;
    g.t0=c.t0;
    loadPass(c.run,ST_RUN);
}
static void saveConfig(void){
    cfg_t c;
    c.magic=CFG_MAGIC;
    c.run=g.st==ST_RUN?g.passNo:NO_PASS;
    c.used=g.used;
    c.generation=g.ix.generation;
    c.t0=g.t0;
    c.exitMs=A->ticks_ms();
    A->cfg_save((const uint8_t *)&c,sizeof(c));
}

/* ---- drawing ----
 * Small font: 7 px a glyph, 18 a row. The framebuffer starts below the
 * status bar: y 0-55. The sky plot background (rings, N/E/S/W, dashed path)
 * comes drawn from the host; the app adds the solid part and the markers. */

/* Satellite dot (5x5, rounded) or rise/set square (3x3), as short lines. */
static void mark(const app_api_t *a,uint8_t x,uint8_t y,bool dot){
    int8_t r=dot?2:1;
    for(int8_t dy=-r;dy<=r;dy++){
        int8_t w=(dot && (dy==-2 || dy==2))?1:r;
        a->draw_line(a->fb,x-w,y+dy,x+w,y+dy,true);
    }
}
/* Browse arrows in the status bar: UP/DOWN stacked, or LEFT/RIGHT. */
static const uint8_t arrows[7]={0x14,0x36,0x14, 0x08,0x1C, 0x1C,0x08};
/* Meter: 80 dB onto 30 px (x3>>3, no division), DBM_LO..DBM_LO+80.
 * -133..-53 matches the radio's IARU S-meter (S9 = -93 dBm, top = S9+40)
 * and puts S9 on the middle pixel. */
#define DBM_LO  (-133)
#define F(...) a->format(g.text,sizeof(g.text),__VA_ARGS__)
static void row(const app_api_t *a,uint8_t line){ a->print_normal(g.text,0,0,line); }
static void tiny(const app_api_t *a,uint8_t x,uint8_t y,bool status){ a->print_tiny(g.text,x,y,status,true); }

static void draw(uint8_t rf){
    const app_api_t *a=A;
    a->display_clear();
    a->status_clear();
    a->draw_battery();
    a->print_inverse("SAT TRACK",2,0,true,true,38);
    uint8_t x=44;                                /* SQL goes after the right-arrow slot */
    if(g.st==ST_EMPTY){
        a->print_normal(g.ix.magic==SAT_INDEX_MAGIC?"ALL FLOWN":"NO PASSES",0,127,2);
    } else {
        x=(uint8_t)(x+F("%u/%u",g.passNo+1u,g.ix.count)*4u);
        tiny(a,44,1,true);
        if(g.st!=ST_RUN){                        /* browse hint, per SetNav */
            uint8_t *sl=a->status_line;
            if(g.k1)
                for(uint8_t i=0;i<2;i++){ sl[41+i]|=arrows[3+i]; sl[x+i]|=arrows[5+i]; }
            else
                for(uint8_t i=0;i<3;i++) sl[40+i]|=arrows[i];
        }
        sat_ext_t e;
        a->sat_read(SAT_PASS_OFF(g.passNo)+SAT_EXT_OFF,&e,sizeof(e));
        /* plot background first: the text and lines below are drawn over it */
        for(uint8_t pg=0;pg<SAT_FRAME_PAGES;pg++)
            a->sat_read(SAT_PASS_OFF(g.passNo)+SAT_FRAME_OFF+pg*SAT_FRAME_W,&a->fb[pg+1][SAT_FRAME_X],SAT_FRAME_W);

        a->print_bold(g.P.name,0,0,0);
        /* bold marks the live frequency: RX, or TX while keyed */
        bool keyed=(rf==APP_SAT_TX);
        for(uint8_t i=0;i<2;i++){
            uint32_t f=i?g.tx:g.rx;
            if(!f) break;
            F("%cX %u.%05u",i?'T':'R',f/100000u,f%100000u);
            ((i==keyed)?a->print_bold:a->print_normal)(g.text,0,0,(uint8_t)(i+1u));
        }
        F("EL%02u/%02u",g.el,g.P.max_el); tiny(a,98,1,false);
        F("AZ%03u",g.az); tiny(a,106,49,false);

        uint32_t end=endMs(), t=g.tMs>0?(uint32_t)g.tMs:0;
        if(g.st==ST_RUN){
            uint32_t tca=(uint32_t)e.tca_s*1000u;
            if(t<tca){ uint32_t r=(tca-t)/1000u; F("TCA %02u:%02u",r/60u,r%60u); row(a,3); }
            uint32_t r=t<end?(end-t)/1000u:0;
            F("LOS %02u:%02u",r/60u,r%60u); row(a,4);
            if(g.txDenied) F("TX DENIED");
            else if(keyed) F("TX %s",a->sat_power());
            else if(g.trim) F("TRIM %+dHz",g.trim*10);
            else {                               /* absolute meter, see DBM_LO */
                int16_t dbm=a->rssi_dbm(), v=(int16_t)(dbm-DBM_LO);
                F("%ddBm",dbm);
                uint8_t w=v<0?0:v>80?30:(uint8_t)(((uint16_t)v*3u)>>3);
                if(w>g.pk) g.pk=w;
                /* direct to line 6: 0x22 box, 0x3E filled, 0x7F peak */
                uint8_t *p=&a->fb[6][52];
                for(uint8_t x=0;x<32;x++) p[x]|=(x<=w || x==31)?0x3E:0x22;
                p[g.pk]|=0x7F;
            }
            row(a,6);
        } else {
            a->print_tiny(e.aos,0,25,false,true);
            a->print_tiny(e.los,0,32,false,true);
            /* bottom row stays left of the plot's S (x < 100) */
            if(g.st==ST_DONE){
                F("%s MENU=NEXT",g.stopped?"STOP":"LOS ");
                if(g.stopped) a->print_tiny("0=RESUME",0,40,false,true);
            } else {
                F("MENU AT AOS");
                if(g.used&(1u<<g.passNo)) a->print_tiny("FLOWN",0,40,false,true);
            }
            row(a,6);
        }

        /* sky plot */
        uint8_t n=e.n_plot;
        if((uint8_t)(n-2u)<=SAT_PLOT_POINTS-2u){
            uint8_t k=(uint8_t)(n-1u);           /* solid up to here */
            if(g.st==ST_RUN && t/100u<end/100u) k=(uint8_t)((t/100u)*(n-1u)/(end/100u));
            for(uint8_t i=0;i<k;i++)
                a->draw_line(a->fb,e.plot[i][0],e.plot[i][1],e.plot[i+1][0],e.plot[i+1][1],true);
            uint8_t m=g.st==ST_ARMED?0:k;
            mark(a,e.plot[m][0],e.plot[m][1],g.st==ST_RUN);
        }
    }
    F("SQL%u",g.sql); tiny(a,(uint8_t)(x+4),1,true);
    a->blit_status();
    a->blit_full();
}

/* ---- keys ---- */
static void onKey(uint8_t key,bool repeat){
    int8_t dir=A->nav_dir(key);
    if(repeat){
        if(!dir || g.fheld) return;               /* only UP/DOWN repeat */
    } else {
        if(key==APP_KEY_F){ g.fkey=!g.fkey; return; }
        if(g.fkey){
            g.fkey=false;
            if(dir){ g.sql=A->sat_squelch(dir); g.fheld=true; return; }
        }
    }
    switch(g.st){
        case ST_ARMED:
            if(key==APP_KEY_MENU){ g.t0=A->ticks_ms(); g.st=ST_RUN; A->play_tone(1000,60); }
            else if(dir) loadPass(findPass(g.passNo,dir,false),ST_ARMED);
            else if(key==APP_KEY_0) g.used^=(uint16_t)(1u<<g.passNo);      /* flown <-> not */
            break;
        case ST_RUN:
            if(dir) g.t0-=(uint32_t)(dir*1000);
            else if(key==APP_KEY_0){ g.used|=(uint16_t)(1u<<g.passNo); g.st=ST_DONE; g.stopped=true; }
            else if(key==APP_KEY_3 || key==APP_KEY_9){
                int16_t s=g.P.modulation?1:10;                 /* 10 Hz SSB, 100 Hz FM */
                int16_t t=(int16_t)(g.trim+(key==APP_KEY_3?s:-s));
                if(uabs(t)<=TRIM_MAX) g.trim=t;
            } else if(key==APP_KEY_5){
                resync();
            }
            break;
        case ST_DONE:
            if(key==APP_KEY_MENU) loadPass(findPass(g.passNo,1,true),ST_ARMED);
            else if(key==APP_KEY_0 && g.stopped){                  /* resume: t0 still holds */
                g.used&=(uint16_t)~(1u<<g.passNo);
                g.st=ST_RUN; g.stopped=false;
            }
            break;
        default:
            if(dir) loadPass(findPass(g.passNo,dir,false),ST_ARMED);
            break;
    }
}

/* ---- entry ---- */
__attribute__((section(".text.entry"), used))
void app_main(const app_api_t *api){
    g.api=api;
    g.k1=A->nav_dir(APP_KEY_UP)<0;              /* SetNav LEFT/RIGHT (UV-K1) */
    A->sat_enter();
    g.sql=A->sat_squelch(0);
    if(loadIndex()){
        loadConfig();
        if(g.st==ST_EMPTY) loadPass(findPass(SAT_PASS_COUNT-1u,1,true),ST_ARMED);
    }
    A->backlight_on();

    uint8_t held=APP_KEY_INVALID, rf=APP_SAT_IDLE, batt=0;
    uint16_t heldMs=0;
    bool ptt=false, running=true;
    while(running){
        uint8_t key=A->get_key();
        if(key==APP_KEY_WAKE) key=APP_KEY_INVALID;

        if(key==APP_KEY_PTT){
            if(!ptt){
                ptt=true;
                if(g.st==ST_RUN){ compute(); g.txDenied=A->sat_ptt(true,g.tx)!=0; }
                else g.txDenied=true;
            }
        } else if(ptt){
            ptt=false; g.txDenied=false;
            A->sat_ptt(false,0);
            g.tuned=0;                          /* RX was reset: retune now */
        }

        if(key!=APP_KEY_SAVER){
            if(key==APP_KEY_INVALID || key==APP_KEY_PTT){
                if(held==APP_KEY_EXIT) running=false;
                held=APP_KEY_INVALID; heldMs=0; g.fheld=false;
            } else if(key!=held){
                held=key; heldMs=0;
                if(key!=APP_KEY_EXIT) onKey(key,false);
            } else if((heldMs=(uint16_t)(heldMs+TICK_MS))>=REPEAT_MS){
                heldMs=0;
                onKey(key,true);
            }
        }

        if(g.st!=ST_EMPTY){
            compute();
            if(rf!=APP_SAT_TX) retune(g.tuned==0);
        }
        rf=A->sat_tick();
        if(rf!=APP_SAT_IDLE) A->backlight_on();
        if(key!=APP_KEY_SAVER) draw(rf);
        if(rf==APP_SAT_TX) batt=0;
        else if(++batt>=50){ batt=0; A->battery_sample(); }
        A->delay_ms(TICK_MS);
        A->backlight_update();
    }
    if(ptt) A->sat_ptt(false,0);
    saveConfig();
    A->sat_leave();
}
