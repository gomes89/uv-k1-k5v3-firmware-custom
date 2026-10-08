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
 * Sat Track pass store, shared by the firmware (UART upload, sat_read) and the
 * app. External flash 0x130000-0x141000, between the app slots and the voice
 * data: sector 0 holds the index, sectors 1-16 one pass each (64-byte header,
 * then the samples from AOS, then the display extras at SAT_EXT_OFF). Passes
 * are written in AOS order.
 */

#ifndef APPS_SAT_FORMAT_H
#define APPS_SAT_FORMAT_H

#include <stdint.h>

#define SAT_STORE_BASE    0x00130000u
#define SAT_SECTOR_SIZE   0x00001000u
#define SAT_PASS_COUNT    16u
#define SAT_SECTOR_COUNT  (1u + SAT_PASS_COUNT)
#define SAT_STORE_SIZE    (SAT_SECTOR_SIZE * SAT_SECTOR_COUNT)
#define SAT_PASS_OFF(n)   (SAT_SECTOR_SIZE * (1u + (uint32_t)(n)))

#define SAT_INDEX_MAGIC   0x49544153u   /* "SATI" */
#define SAT_PASS_MAGIC    0x50544153u   /* "SATP" */
#define SAT_FMT_VERSION   4u
#define SAT_HDR_SIZE      64u
#define SAT_EXT_OFF       0x0E00u       /* display extras, fixed spot in the sector */
#define SAT_MAX_SAMPLES   ((SAT_EXT_OFF - SAT_HDR_SIZE) / 4u)
#define SAT_PLOT_POINTS   64u           /* sky plot path, evenly spaced in time */
#define SAT_PLOT_X        104           /* sky plot centre in framebuffer pixels */
#define SAT_PLOT_Y        28
#define SAT_PLOT_R        17            /* horizon radius in x px; y is squashed */
/* Plot background drawn by the host (rings, N/E/S/W, dashed path), copied as is
 * into framebuffer pages 1-6, columns SAT_FRAME_X..+SAT_FRAME_W-1. */
#define SAT_FRAME_X       82u
#define SAT_FRAME_W       46u
#define SAT_FRAME_PAGES   6u
#define SAT_FRAME_OFF     (SAT_EXT_OFF + 167u)

#define SAT_C_Q           1199169832u   /* speed of light in 0.25 m/s */

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint32_t generation;   /* changes at every upload */
    int16_t  clock_ppm;    /* radio clock correction, + = slow */
    uint8_t  count;
    uint8_t  version;
    uint16_t reserved;
    uint16_t crc;          /* CRC-16/XMODEM of the 14 bytes above */
} sat_index_t;

typedef struct __attribute__((packed)) {
    int16_t rr;            /* range rate, 0.25 m/s, + = receding */
    uint8_t az;            /* 360/256 deg */
    uint8_t el;            /* 0.5 deg */
} sat_sample_t;

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint32_t dl_10hz;
    uint32_t ul_10hz;      /* 0 = listen only */
    uint16_t n_samples;
    uint16_t ctcss_01hz;   /* 885 = 88.5 Hz, 0 = none */
    uint8_t  step_s;       /* 1-4 s between samples */
    uint8_t  modulation;   /* 0 FM, 1 AM, 2 USB */
    uint8_t  max_el;
    uint8_t  version;
    uint16_t crc;          /* CRC-16/XMODEM of the header (crc = 0) and samples */
    char     name[10];
    char     label[12];    /* AOS in local time, e.g. "Tu 14:32:10" */
    uint32_t k_dl;         /* dl * 2^20 / SAT_C_Q (Doppler factors, computed by the host) */
    uint32_t k_ul;         /* ul * 2^20 / SAT_C_Q, 0 = listen only */
    uint32_t q8_dl;        /* SAT_C_Q * 2^8 / dl */
    uint8_t  reserved[8];
} sat_pass_t;

/* Computed by the host so the app only draws. Covered by the pass CRC. */
typedef struct __attribute__((packed)) {
    char     aos[18];      /* shown as is, e.g. "AOS Tue 14:32:10" */
    char     los[18];
    uint16_t tca_s;        /* highest point, seconds after AOS */
    uint8_t  n_plot;       /* points used in plot[], 2..SAT_PLOT_POINTS */
    uint8_t  plot[SAT_PLOT_POINTS][2];  /* framebuffer x, y; N up, E right */
} sat_ext_t;               /* followed by the frame, SAT_FRAME_W x SAT_FRAME_PAGES bytes */

_Static_assert(sizeof(sat_index_t) == 16u, "sat_index_t");
_Static_assert(sizeof(sat_sample_t) == 4u, "sat_sample_t");
_Static_assert(sizeof(sat_pass_t) == SAT_HDR_SIZE, "sat_pass_t");
_Static_assert(sizeof(sat_ext_t) == 167u, "sat_ext_t");
_Static_assert(SAT_FRAME_OFF + SAT_FRAME_W * SAT_FRAME_PAGES <= SAT_SECTOR_SIZE, "frame fits");

#endif
