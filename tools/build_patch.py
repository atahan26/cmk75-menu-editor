"""Build the "live screen" patch for the RY5050 screen-chip firmware (sample: ROYALAXE oled v108).

Live uploads are marked by the PC: header frame delay == LIVE_DELAY and frame index == LIVE_SLOT.
For those uploads the patched firmware draws each 4 KB block straight to the LCD and skips every flash
write (picture blocks, slot tables, settings save) and the page switch / repaint that a commit causes.
All other uploads behave exactly as before.

Safety net (boot confirmation, on unless --no-net): right after the clock setup in main, the firmware
writes the bootloader's "update requested" flag (AA 55 AA 55 at 0x01000120). It erases it again once it
has run DISARM_TICKS ticks AND processed a valid message from the keyboard chip (0x000210E6 = 1). If the
firmware hangs or crashes before that, the next power-on stays in the bootloader, ready for a re-flash.

usage: build_patch.py IN_firmwareOledFile.bin OUT_firmwareOledFile.bin [--no-net]
"""
from __future__ import annotations

import sys
from pathlib import Path

from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs
from keystone import KS_ARCH_ARM, KS_MODE_THUMB, Ks

IMAGE_BASE = 0x01000200
# Live marker = frame count 1 + frame delay LIVE_DELAY + frame index LIVE_SLOT. The vendor app always sends
# delay 0 for still pictures; GIFs (frame count > 1) can never match. 0x4C was refused by the real CMK75
# (handshake reply 4), delay 1 was accepted (2026-11-03).
LIVE_DELAY, LIVE_SLOT = 0x01, 4

# RAM addresses (screen app v108)
UPLOAD_FRAMES = 0x0002008F       # header byte 4 (frame count, 1 for a still picture)
UPLOAD_DELAY = 0x00020090        # header byte 5 (frame delay)
UPLOAD_FRAME = 0x0002008A        # frame index from the data frames (= slot for still pictures)
UPLOAD_REGION = 0x0002008B       # left, top, width, height (bytes)
BLOCK_NO = 0x000200BB            # current block number (u32, byte-packed; 1-based)
TOTAL_LEN = 0x000210C6           # declared upload length (u32, byte-packed), from the header
BLOCK_BUF = 0x000200C4           # 4 KB block buffer
DATA_LR_VALUE = 0x000210C0       # value the data handler keeps in lr (block-ready flag base)
PROGRESS_ACTIVE = 0x0002007F     # followed by 0x00020080 "upload started"
DISPLAY_ENABLED = 0x000210C5     # pages only redraw while this is 1; an upload clears it

# Firmware functions
SET_WINDOW = 0x0101071E          # (x, y, w, h) -> CASET/RASET/RAMWR
LCD_DATA_BUF = 0x010106EA        # (buf, len)

# Hook sites: address -> (expected original instruction text, continuation addresses)
HOOKS = {
    0x01014D3A: ["cmp r0, #5", "beq #0x1014dfa"],                 # P1 picture writer
    0x010124F0: ["ldrb r2, [r6, #0xa]", "strb r2, [r0, #4]"],     # P2 data handler (still path)
    0x01012AF8: ["ldr r6, [sp, #4]", "movs r3, #1"],              # P3 commit handler
    0x010104C4: ["bl #0x1011c00"],                                 # P4 main loop -> progress-screen task
}
PROGRESS_TASK = 0x01011C00

# Boot confirmation (P5)
NET_HOOK = 0x01010472            # main: "bl 0x01010458", the second init call, right after the clock setup
NET_HOOK_TARGET = 0x01010458
FLAG_ADDR = 0x01000120           # bootloader stays in update mode while this holds anything but FF FF FF FF
INT_ERASE, INT_PROGRAM = 0x01015388, 0x01015448      # vendor routines (ROM flash API), same calls as 0x0101234E
NET_STATE = 0x00027F00           # our RAM: u32 state (1 = armed, 2 = disarmed), u32 tick counter at +4
# Live hold: every live commit pauses the page/clock drawing tasks for LIVE_TIMEOUT ticks (the clock would
# otherwise repaint its digits over the PC's picture every second). Countdown u32 in our RAM.
LIVE_TICKS, LIVE_TIMEOUT = NET_STATE + 24, 0x7FFFFFFF   # v10: the hold lasts until released (record x = 255)
STATE_BYTES = 196                # our RAM block 0x27F00.. cleared at boot
FLASH_UNTIL, FLASH_ON = NET_STATE + 0x90, NET_STATE + 0x94   # v14 press feedback: u32 end time (ms), u8 active
FLASH_MS, FLASH_COLOUR = 120, 0xFFFF
# Screen-side navigation (v11): +0 on, +1 index, +2 count, +3 frame thickness, +4 u16 accent, +6 u16 background,
# +8 u16 inactive colour, +12 cells (x, y, w, h) x NAV_MAX. Set by the PC's layout record (0xFE).
NAV, NAV_MAX = NET_STATE + 0x40, 16   # +10: s8 knob steps waiting to be drawn (v12: drawn in the main loop)
WDT_FEED = 0x010147B0            # stock watchdog check-in (0x5937 -> 0x000F0008); clobbers r0, r1
MS_COUNTER = 0x00020048          # u32, incremented by the timer ISR every tick (1 ms)
# v17 stored menu. Flash area 0x7A0000..0x7FFFFF (the stored-picture slots): sector 0 = directory
# ("MENU", version, page count; 128-byte entries from offset 16), pages in later sectors as RECT blocks.
# Entry: +0 first sector, +1 sector count, +2 cells, +3 frame thickness, +4 flags (bit0 wrap), +5 next page,
# +6 previous page (0xFF none), +8 accent, +10 background, +12 inactive (RGB565 big endian),
# +16 cells 16 x (x, y, w, h), +80 targets 16 (page to open on a press, 0xFF = PC action), +96 target cells 16.
MENU_BASE, MENU_END = 0x007A0000, 0x00800000
FL_ERASE, FL_PAGE_PROG, FL_READ = 0x01014B5A, 0x01014BE0, 0x01014C6C   # (addr) / (buf, addr, len<=256) / (buf, addr, len)
ENTRY_BUF = 0x00027E00           # 112-byte scratch for one directory entry (free RAM below our block)
MENU = NET_STATE + 0x98          # +0 on, +1 page, +2 next, +3 prev, +4 targets[16], +20 target cells[16],
MENU_REQ = NET_STATE + 0xBC      # show-page request: +0 page, +1 cell, +2 pending
SP_SEC = NET_STATE + 0xC0        # u8 sector loop counter of show_page
SWRTC, SW_LAST = NET_STATE + 32, NET_STATE + 44  # software clock: 9 'RTC registers' (BCD), u32 last ms
LIVE_COUNT, DBG_COUNT = NET_STATE + 28, NET_STATE + 29   # u8 'live block drawn' flag, u8 debug tick counter
LINK_OK = 0x000210E6             # becomes 1 once a valid message from the keyboard chip has been processed
DISARM_TICKS = 3000

FILL_RECT = 0x0101077C           # (x, y, w, h, [sp] = RGB565)
LCD_DATA = 0x010106D8            # (byte) one data byte to the LCD

NAV_FUNCS = [("nav_frame", f"""
    @ r0 = cell index, r1 = RGB565 colour: draw that cell's frame (4 bars of NAV thickness).
    push {{r4, r5, r6, r7, lr}}
    sub  sp, #8
    ldr  r4, ={NAV:#x}
    ldrb r2, [r4, #2]
    cmp  r0, r2
    bhs  nf_done
    lsls r0, r0, #2
    adds r0, #12
    adds r5, r4, r0               @ cell
    mov  r6, r1
    ldrb r7, [r4, #3]             @ thickness
    cmp  r7, #0
    beq  nf_done
    str  r6, [sp]
    ldrb r0, [r5, #0]
    ldrb r1, [r5, #1]
    ldrb r2, [r5, #2]
    mov  r3, r7
    bl   {FILL_RECT:#x}           @ top
    str  r6, [sp]
    ldrb r0, [r5, #0]
    ldrb r1, [r5, #1]
    ldrb r3, [r5, #3]
    adds r1, r1, r3
    subs r1, r1, r7
    ldrb r2, [r5, #2]
    mov  r3, r7
    bl   {FILL_RECT:#x}           @ bottom
    str  r6, [sp]
    ldrb r0, [r5, #0]
    ldrb r1, [r5, #1]
    adds r1, r1, r7
    mov  r2, r7
    ldrb r3, [r5, #3]
    subs r3, r3, r7
    subs r3, r3, r7
    bl   {FILL_RECT:#x}           @ left
    str  r6, [sp]
    ldrb r0, [r5, #0]
    ldrb r2, [r5, #2]
    adds r0, r0, r2
    subs r0, r0, r7
    ldrb r1, [r5, #1]
    adds r1, r1, r7
    mov  r2, r7
    ldrb r3, [r5, #3]
    subs r3, r3, r7
    subs r3, r3, r7
    bl   {FILL_RECT:#x}           @ right
nf_done:
    add  sp, #8
    pop  {{r4, r5, r6, r7, pc}}
"""), ("nav_redraw", f"""
    @ Erase every cell's frame, then draw the selected one (accent when on, inactive colour when off).
    push {{r4, r5, lr}}
    ldr  r4, ={NAV:#x}
    movs r5, #0
nr_loop:
    bl   {WDT_FEED:#x}
    ldrb r0, [r4, #2]
    cmp  r5, r0
    bhs  nr_sel
    mov  r0, r5
    ldrh r1, [r4, #6]
    bl   {{nav_frame}}
    adds r5, #1
    b    nr_loop
nr_sel:
    ldrh r1, [r4, #8]
    ldrb r0, [r4, #0]
    cmp  r0, #0
    beq  nr_draw
    ldrh r1, [r4, #4]
nr_draw:
    ldrb r0, [r4, #1]
    bl   {{nav_frame}}
    pop  {{r4, r5, pc}}
"""), ("nav_move", f"""
    @ r0 = +1 / -1 (knob turn reported by the keyboard chip). Moves the frame inside the current page only;
    @ page changes are drawn by the PC.
    push {{r4, r5, lr}}
    ldr  r4, ={NAV:#x}
    ldrb r1, [r4, #0]
    cmp  r1, #0
    beq  nm_done
    ldrb r5, [r4, #1]
    adds r0, r5, r0
    ldrb r1, [r4, #2]
    cmp  r0, r1
    blo  nm_ok
    @ v17 stored menu: past the end -> next page, before the start -> previous page (if linked)
    ldr  r2, ={MENU:#x}
    ldrb r3, [r2, #0]
    cmp  r3, #0
    beq  nm_wrapchk
    cmp  r0, #0
    bge  nm_pg_next               @ signed: past the end
    ldrb r3, [r2, #3]             @ previous page
    b    nm_pg
nm_pg_next:
    ldrb r3, [r2, #2]             @ next page
nm_pg:
    cmp  r3, #0xFF
    beq  nm_wrapchk               @ no link: wrap rule (r0, r1 untouched)
    movs r1, #0
    cmp  r0, #0
    bge  nm_pg_go
    movs r1, #0xFF                @ going back: land on the last cell
nm_pg_go:
    mov  r0, r3
    bl   {{show_page}}
    b    nm_done
nm_wrapchk:
    @ past either end: wrap only if the layout says the whole list is on this page (v16, NAV+11)
    ldrb r2, [r4, #11]
    cmp  r2, #0
    beq  nm_done
    cmp  r1, #0
    beq  nm_done
    movs r2, #0
    cmp  r0, r2
    bge  nm_first                 @ signed: past the end -> first cell
    subs r0, r1, #1               @ before the first -> last cell
    b    nm_ok
nm_first:
    movs r0, #0
nm_ok:
    push {{r0}}
    mov  r0, r5
    ldrh r1, [r4, #6]
    bl   {{nav_frame}}             @ old frame -> background
    pop  {{r0}}
    strb r0, [r4, #1]
    ldrh r1, [r4, #4]
    bl   {{nav_frame}}             @ new frame -> accent
nm_done:
    pop  {{r4, r5, pc}}
"""), ("nav_tick", f"""
    @ Once per main-loop tick: draw the knob steps the frame dispatcher recorded (kind1_hook). Drawing here
    @ instead of inside the dispatcher keeps the serial-link handling as quick as stock (v11 reset on knob turns).
    push {{r4, lr}}
    ldr  r1, ={MENU_REQ:#x}
    ldrb r0, [r1, #2]
    cmp  r0, #0
    beq  nt_noreq
    ldr  r0, ={DISPLAY_ENABLED:#x}
    ldrb r0, [r0]
    cmp  r0, #0
    beq  nt_noreq                 @ wait until the upload has finished
    movs r0, #0
    strb r0, [r1, #2]
    ldr  r2, ={NAV:#x}
    strb r0, [r2, #10]            @ the PC's position wins: drop steps not drawn yet
    ldrb r0, [r1, #0]
    ldrb r1, [r1, #1]
    bl   {{show_page}}             @ requested by the PC (rec_show)
nt_noreq:
    ldr  r4, ={NAV:#x}
nt_loop:
    movs r0, #10
    ldrsb r1, [r4, r0]
    cmp  r1, #0
    beq  nt_done
    bgt  nt_pos
    adds r1, #1
    strb r1, [r4, #10]
    movs r0, #0
    subs r0, #1
    bl   {{nav_move}}
    b    nt_loop
nt_pos:
    subs r1, #1
    strb r1, [r4, #10]
    movs r0, #1
    bl   {{nav_move}}
    b    nt_loop
nt_done:
    @ Press feedback: flash the selected frame white for FLASH_MS, then back to the accent colour.
    ldr  r1, ={FLASH_ON:#x}
    ldrb r0, [r1]
    cmp  r0, #0
    beq  nt_out
    cmp  r0, #1
    bne  nt_wait
    ldr  r2, ={MENU:#x}
    ldrb r0, [r2, #0]
    cmp  r0, #0
    beq  nt_flash
    ldrb r3, [r4, #1]             @ stored menu: does this cell open a page?
    adds r3, r2, r3
    ldrb r0, [r3, #4]
    cmp  r0, #0xFF
    beq  nt_flash
    movs r2, #0
    strb r2, [r1]                 @ no flash: the new page is the feedback
    ldrb r1, [r3, #20]
    bl   {{show_page}}
    b    nt_out
nt_flash:
    ldr  r1, ={FLASH_ON:#x}
    movs r0, #2
    strb r0, [r1]                 @ 1 = requested by kind1_hook -> draw white now
    ldr  r2, ={MS_COUNTER:#x}
    ldr  r0, [r2]
    ldr  r2, ={FLASH_MS}
    adds r0, r0, r2
    ldr  r1, ={FLASH_UNTIL:#x}
    str  r0, [r1]
    ldrb r0, [r4, #1]
    ldr  r1, ={FLASH_COLOUR:#x}
    bl   {{nav_frame}}
    b    nt_out
nt_wait:
    ldr  r2, ={MS_COUNTER:#x}
    ldr  r0, [r2]
    ldr  r1, ={FLASH_UNTIL:#x}
    ldr  r1, [r1]
    subs r0, r0, r1
    bmi  nt_out                   @ not yet (signed: survives counter wrap)
    ldr  r1, ={FLASH_ON:#x}
    movs r0, #0
    strb r0, [r1]
    ldrb r0, [r4, #0]
    cmp  r0, #0
    beq  nt_out                   @ menu mode ended meanwhile: the PC repaints
    ldrb r0, [r4, #1]
    ldrh r1, [r4, #4]
    bl   {{nav_frame}}             @ back to the accent colour
nt_out:
    pop  {{r4, pc}}
"""), ("rec_layout", f"""
    @ Layout record [0xFE, on, index, count, thickness, accent hi, lo, bg hi, lo, inactive hi, lo,
    @ count x (x, y, w, h)]. r0 = record, r1 = block end. Returns the next record, or 0 if malformed.
    push {{r4, r5, r6, lr}}
    mov  r4, r0
    mov  r2, r4
    adds r2, #11                  @ (not "adds r2, r4, #11": the 3-register form only takes 0..7 -> Thumb-2)
    cmp  r2, r1
    bhi  rl_bad
    ldrb r3, [r4, #3]
    cmp  r3, #{NAV_MAX}
    bhi  rl_bad
    lsls r5, r3, #2
    adds r2, r2, r5
    cmp  r2, r1
    bhi  rl_bad
    mov  r6, r2                   @ next record
    ldr  r5, ={NAV:#x}
    ldrb r0, [r4, #1]
    lsrs r2, r0, #1
    movs r1, #1
    ands r0, r1                   @ bit 0: frame moves on
    ands r2, r1                   @ bit 1: wrap around (the whole list is on this page)
    strb r0, [r5, #0]
    strb r2, [r5, #11]
    ldrb r0, [r4, #2]
    strb r0, [r5, #1]
    strb r3, [r5, #2]
    movs r0, #0
    strb r0, [r5, #10]            @ forget knob steps not drawn yet: the PC's position wins
    ldr  r2, ={FLASH_ON:#x}
    strb r0, [r2]                 @ and any press flash in progress
    ldrb r0, [r4, #4]
    strb r0, [r5, #3]
    ldrb r0, [r4, #5]
    lsls r0, r0, #8
    ldrb r2, [r4, #6]
    orrs r0, r2
    strh r0, [r5, #4]
    ldrb r0, [r4, #7]
    lsls r0, r0, #8
    ldrb r2, [r4, #8]
    orrs r0, r2
    strh r0, [r5, #6]
    ldrb r0, [r4, #9]
    lsls r0, r0, #8
    ldrb r2, [r4, #10]
    orrs r0, r2
    strh r0, [r5, #8]
    adds r4, #11
    adds r5, #12
    lsls r3, r3, #2
    movs r2, #0
rl_copy:
    cmp  r2, r3
    bhs  rl_ok
    ldrb r0, [r4, r2]
    strb r0, [r5, r2]
    adds r2, #1
    b    rl_copy
rl_ok:
    bl   {{nav_redraw}}
    mov  r0, r6
    pop  {{r4, r5, r6, pc}}
rl_bad:
    movs r0, #0
    pop  {{r4, r5, r6, pc}}
"""), ("rec_packbits", f"""
    @ Compressed pixels [0xFD, x, y, w, h, len lo, len hi, PackBits data]: control c < 128: c + 1 literal
    @ RGB565 pixels follow; c > 128: the next pixel repeats 257 - c times; 128: nothing.
    @ r0 = record, r1 = block end. Returns the next record, or 0 if malformed.
    push {{r4, r5, r6, r7, lr}}
    mov  r4, r0
    adds r2, r4, #7
    cmp  r2, r1
    bhi  pb_bad
    ldrb r5, [r4, #5]
    ldrb r3, [r4, #6]
    lsls r3, r3, #8
    orrs r5, r3
    adds r6, r2, r5               @ record end
    cmp  r6, r1
    bhi  pb_bad
    ldrb r0, [r4, #1]
    ldrb r1, [r4, #2]
    ldrb r2, [r4, #3]
    ldrb r3, [r4, #4]
    bl   {SET_WINDOW:#x}
    adds r4, #7
pb_loop:
    bl   {WDT_FEED:#x}
    cmp  r4, r6
    bhs  pb_ok
    ldrb r7, [r4]
    adds r4, #1
    cmp  r7, #128
    blo  pb_lit
    beq  pb_loop
    adds r0, r4, #2
    cmp  r0, r6
    bhi  pb_ok
    movs r0, #0xFF
    adds r0, #2                   @ 257
    subs r7, r0, r7               @ repeat count
    ldrb r5, [r4, #0]
    lsls r5, r5, #8
    ldrb r0, [r4, #1]
    orrs r5, r0
    adds r4, #2
pb_rep:
    lsrs r0, r5, #8
    bl   {LCD_DATA:#x}
    uxtb r0, r5
    bl   {LCD_DATA:#x}
    subs r7, #1
    bne  pb_rep
    b    pb_loop
pb_lit:
    adds r7, #1
    lsls r7, r7, #1               @ literal bytes
    adds r0, r4, r7
    cmp  r0, r6
    bhi  pb_ok
    mov  r0, r4
    mov  r1, r7
    bl   {LCD_DATA_BUF:#x}
    adds r4, r4, r7
    b    pb_loop
pb_ok:
    mov  r0, r6
    pop  {{r4, r5, r6, r7, pc}}
pb_bad:
    movs r0, #0
    pop  {{r4, r5, r6, r7, pc}}
""")]

MENU_FUNCS = [("draw_records", f"""
    @ r0 = first record, r1 = end: draw raw (x, y, w, h, pixels) and PackBits (0xFD) records, as in an upload.
    push {{r4, r5, r6, r7, lr}}
    mov  r5, r0
    mov  r6, r1
dr_loop:
    bl   {WDT_FEED:#x}
    adds r0, r5, #4
    cmp  r0, r6
    bhi  dr_done
    ldrb r0, [r5, #0]
    cmp  r0, #0xFD
    bne  dr_raw
    mov  r0, r5
    mov  r1, r6
    bl   {{rec_packbits}}
    cmp  r0, #0
    beq  dr_done
    mov  r5, r0
    b    dr_loop
dr_raw:
    ldrb r2, [r5, #2]
    ldrb r3, [r5, #3]
    cmp  r2, #0
    beq  dr_done
    cmp  r3, #0
    beq  dr_done
    mov  r7, r2
    muls r7, r3, r7
    lsls r7, r7, #1
    adds r0, r5, #4
    adds r0, r0, r7
    cmp  r0, r6
    bhi  dr_done
    ldrb r0, [r5, #0]
    ldrb r1, [r5, #1]
    bl   {SET_WINDOW:#x}
    adds r0, r5, #4
    mov  r1, r7
    bl   {LCD_DATA_BUF:#x}
    adds r5, #4
    adds r5, r5, r7
    b    dr_loop
dr_done:
    pop  {{r4, r5, r6, r7, pc}}
"""), ("show_page", f"""
    @ r0 = page, r1 = cell (0xFF = last). Draw a stored page from flash and take over its layout + links.
    @ Main loop only (uses the 4 KB block buffer).
    push {{r4, r5, r6, r7, lr}}
    mov  r6, r0
    mov  r7, r1
    ldr  r0, ={DISPLAY_ENABLED:#x}
    ldrb r0, [r0]
    cmp  r0, #0
    beq  sp_x                   @ an upload is using the block buffer
    ldr  r0, ={ENTRY_BUF:#x}
    ldr  r1, ={MENU_BASE:#x}
    movs r2, #16
    bl   {FL_READ:#x}
    ldr  r4, ={ENTRY_BUF:#x}
    ldrb r0, [r4, #0]
    cmp  r0, #0x4D
    bne  sp_x
    ldrb r0, [r4, #1]
    cmp  r0, #0x45
    bne  sp_x
    ldrb r0, [r4, #2]
    cmp  r0, #0x4E
    bne  sp_x
    ldrb r0, [r4, #3]
    cmp  r0, #0x55
    bne  sp_x                   @ no menu stored
    ldrb r0, [r4, #5]
    cmp  r6, r0
    bhs  sp_x                   @ no such page
    b    sp_read
sp_x:
    b    sp_out                   @ (far exit: a conditional branch cannot reach)
sp_read:
    ldr  r0, ={ENTRY_BUF:#x}
    lsls r1, r6, #7
    ldr  r2, ={MENU_BASE + 16:#x}
    adds r1, r1, r2
    movs r2, #112
    bl   {FL_READ:#x}
    ldr  r1, ={SP_SEC:#x}
    movs r0, #0
    strb r0, [r1]
sp_sec:
    ldr  r1, ={SP_SEC:#x}
    ldrb r0, [r1]
    ldrb r2, [r4, #1]
    cmp  r0, r2
    bhs  sp_layout
    adds r2, r0, #1
    strb r2, [r1]
    ldrb r1, [r4, #0]
    adds r1, r1, r0
    lsls r1, r1, #12
    ldr  r2, ={MENU_BASE:#x}
    adds r1, r1, r2
    ldr  r0, ={BLOCK_BUF:#x}
    ldr  r2, =4096
    bl   {FL_READ:#x}
    bl   {WDT_FEED:#x}
    ldr  r0, ={BLOCK_BUF:#x}
    ldrb r1, [r0, #0]
    cmp  r1, #0x52
    bne  sp_sec                   @ not a RECT block
    adds r0, #4
    ldr  r1, ={BLOCK_BUF + 4096:#x}
    bl   {{draw_records}}
    b    sp_sec
sp_layout:
    ldr  r5, ={NAV:#x}
    ldrb r0, [r4, #2]
    cmp  r0, #{NAV_MAX}
    bls  sp_cnt
    movs r0, #{NAV_MAX}
sp_cnt:
    strb r0, [r5, #2]
    cmp  r0, #0
    bne  sp_cnt_ok
    movs r7, #0
    b    sp_idx
sp_cnt_ok:
    cmp  r7, r0
    blo  sp_idx
    subs r7, r0, #1               @ 0xFF / out of range -> last cell
sp_idx:
    strb r7, [r5, #1]
    ldrb r0, [r4, #3]
    strb r0, [r5, #3]
    ldrb r0, [r4, #4]
    movs r1, #1
    ands r0, r1
    strb r0, [r5, #11]
    ldrb r0, [r4, #8]
    lsls r0, r0, #8
    ldrb r1, [r4, #9]
    orrs r0, r1
    strh r0, [r5, #4]
    ldrb r0, [r4, #10]
    lsls r0, r0, #8
    ldrb r1, [r4, #11]
    orrs r0, r1
    strh r0, [r5, #6]
    ldrb r0, [r4, #12]
    lsls r0, r0, #8
    ldrb r1, [r4, #13]
    orrs r0, r1
    strh r0, [r5, #8]
    movs r0, #0                   @ (knob steps still pending apply to the new page: the PC counts them too)
    ldr  r1, ={FLASH_ON:#x}
    strb r0, [r1]
    mov  r0, r4
    adds r0, #16
    mov  r1, r5
    adds r1, #12
    movs r2, #0
sp_cells:
    cmp  r2, #64
    bhs  sp_tg
    ldrb r3, [r0, r2]
    strb r3, [r1, r2]
    adds r2, #1
    b    sp_cells
sp_tg:
    mov  r0, r4
    adds r0, #80
    ldr  r1, ={MENU + 4:#x}
    movs r2, #0
sp_tgl:
    cmp  r2, #32
    bhs  sp_on
    ldrb r3, [r0, r2]
    strb r3, [r1, r2]
    adds r2, #1
    b    sp_tgl
sp_on:
    movs r0, #1
    strb r0, [r5, #0]             @ frame moves on
    ldr  r1, ={MENU:#x}
    strb r0, [r1, #0]             @ stored-menu mode on
    strb r6, [r1, #1]
    ldrb r0, [r4, #5]
    strb r0, [r1, #2]
    ldrb r0, [r4, #6]
    strb r0, [r1, #3]
    bl   {{nav_redraw}}
sp_out:
    pop  {{r4, r5, r6, r7, pc}}
"""), ("rec_flash", f"""
    @ Record [0xFC, flags, addr hi, mid, lo, n]: program n x 256 bytes (n <= 15) at addr (256-aligned, inside
    @ the menu area only); flags bit0: erase each 4 KB sector when its first page comes. Returns next or 0.
    push {{r4, r5, r6, r7, lr}}
    mov  r4, r0
    mov  r2, r4
    adds r2, #6
    cmp  r2, r1
    bhi  fw_bad
    ldrb r7, [r4, #5]
    cmp  r7, #15
    bhi  fw_bad
    cmp  r7, #0
    beq  fw_bad
    lsls r3, r7, #8
    adds r2, r2, r3
    cmp  r2, r1
    bhi  fw_bad
    ldrb r5, [r4, #2]
    lsls r5, r5, #16
    ldrb r0, [r4, #3]
    lsls r0, r0, #8
    orrs r5, r0
    ldrb r0, [r4, #4]
    orrs r5, r0
    uxtb r0, r5
    cmp  r0, #0
    bne  fw_bad                   @ not 256-aligned
    ldr  r0, ={MENU_BASE:#x}
    cmp  r5, r0
    blo  fw_bad
    lsls r3, r7, #8
    adds r0, r5, r3
    ldr  r1, ={MENU_END:#x}
    cmp  r0, r1
    bhi  fw_bad                   @ never outside the menu area
    ldrb r6, [r4, #1]             @ flags
    adds r4, #6
fw_page:
    movs r0, #1
    tst  r6, r0
    beq  fw_prog
    lsls r0, r5, #20
    bne  fw_prog                  @ not the first page of a sector
    mov  r0, r5
    bl   {FL_ERASE:#x}
fw_prog:
    mov  r0, r4
    mov  r1, r5
    movs r2, #0xFF
    adds r2, #1
    bl   {FL_PAGE_PROG:#x}
    bl   {WDT_FEED:#x}
    adds r4, #0xFF
    adds r4, #1
    adds r5, #0xFF
    adds r5, #1
    subs r7, #1
    bne  fw_page
    mov  r0, r4                   @ next record
    pop  {{r4, r5, r6, r7, pc}}
fw_bad:
    movs r0, #0
    pop  {{r4, r5, r6, r7, pc}}
"""), ("rec_show", f"""
    @ Record [0xFB, page, cell]: show a stored page (done by nav_tick in the main loop). Returns next or 0.
    adds r2, r0, #3
    cmp  r2, r1
    bhi  rs_bad
    ldr  r3, ={MENU_REQ:#x}
    ldrb r1, [r0, #1]
    strb r1, [r3, #0]
    ldrb r1, [r0, #2]
    strb r1, [r3, #1]
    movs r1, #1
    strb r1, [r3, #2]
    mov  r0, r2
    bx   lr
rs_bad:
    movs r0, #0
    bx   lr
""")]

PATCH_FUNCS = [("is_live", """
    @ r0 = 1 if the current upload is live, else 0. Clobbers r0, r1 only.""" + f"""
    ldr  r0, ={UPLOAD_DELAY:#x}
    ldrb r1, [r0]
    cmp  r1, #{LIVE_DELAY:#x}
    bne  not_live
    ldr  r0, ={UPLOAD_FRAMES:#x}
    ldrb r1, [r0]
    cmp  r1, #1
    bne  not_live
    @ No frame-index check: the CMK75 keyboard chip sends its own frame counter there (sometimes minus 1),
    @ not the slot the PC asked for, so live uploads were taken for normal ones on the real keyboard.
    @ The header marker alone (frame count 1 + delay 1) is what the progress-screen gate uses too.
    movs r0, #1
    bx   lr
not_live:
    movs r0, #0
    bx   lr
"""), ("writer_hook", f"""
    @ P1. In: r0 = slot, r4 = 0x200A3. lr = 0x01014D3F
    push {{r0, lr}}
    bl   {{is_live}}
    cmp  r0, #0
    pop  {{r0}}
    bne  w_live                   @ (short branch + b: a far beq would become a Thumb-2 beq.w, not on M0)
    b    w_normal
w_live:
    pop  {{r1}}                   @ drop return address: we leave through the writer's tail
    @ Multi-block live upload (one handshake per picture): the PC packs whole columns into each 4 KB block,
    @ cpb = 4096 // (2 * height) columns per block, padded. Block n (1-based, 0x200BB) holds columns
    @ (n-1)*cpb ... of the region. Back-to-back single-block uploads got merged by the CMK75 keyboard chip.
    push {{r5, r6, r7}}
    @ v15: a new live upload starts (block 1): the screen-side frame work stops until the upload's layout record
    @ arrives, so a late knob step or the press-flash restore cannot draw an old-layout frame into the new page.
    ldr  r0, ={BLOCK_NO:#x}
    ldrb r0, [r0]
    cmp  r0, #1
    bne  w_nav_ok
    ldr  r1, ={NAV:#x}
    movs r0, #0
    strb r0, [r1, #0]             @ frame moves off
    strb r0, [r1, #10]            @ pending knob steps dropped
    ldr  r1, ={FLASH_ON:#x}
    strb r0, [r1]                 @ press flash cancelled (the PC resends the frame area)
    ldr  r1, ={MENU:#x}
    strb r0, [r1]                 @ stored-menu mode off until a show-page request
w_nav_ok:
    @ Rectangle mode: a block starting with "RECT" holds records [x, y, w, h, w*h*2 pixel bytes] (column-major
    @ RGB565, the order set_window + RAMWR expects); w == 0 or the block end stops. Lets the PC send only the
    @ changed pixels (several small rectangles in one upload). Other blocks: column mode below.
    ldr  r5, ={BLOCK_BUF:#x}
    ldrb r0, [r5, #0]
    cmp  r0, #0x52
    bne  w_cols
    ldrb r0, [r5, #1]
    cmp  r0, #0x45
    bne  w_cols
    ldrb r0, [r5, #2]
    cmp  r0, #0x43
    bne  w_cols
    ldrb r0, [r5, #3]
    cmp  r0, #0x54
    bne  w_cols
    ldr  r6, ={BLOCK_BUF + 4096:#x}
    adds r5, #4
r_loop:
    bl   {WDT_FEED:#x}
    adds r0, r5, #4
    cmp  r0, r6
    bhi  r_done
    ldrb r0, [r5, #0]
    cmp  r0, #0xFF
    beq  r_release                @ x = 255: release the live hold (the menu app is quitting)
    cmp  r0, #0xFE
    bne  r_not_layout
    mov  r0, r5
    mov  r1, r6
    bl   {{rec_layout}}            @ x = 254: navigation layout (screen-side frame moves)
    b    r_next
r_not_layout:
    cmp  r0, #0xFC
    bne  r_not_flash
    mov  r0, r5
    mov  r1, r6
    bl   {{rec_flash}}             @ x = 252: write the stored menu to flash
    b    r_next
r_not_flash:
    cmp  r0, #0xFB
    bne  r_not_show
    mov  r0, r5
    mov  r1, r6
    bl   {{rec_show}}              @ x = 251: show a stored page
    b    r_next
r_not_show:
    cmp  r0, #0xFD
    bne  r_pixels
    mov  r0, r5
    mov  r1, r6
    bl   {{rec_packbits}}          @ x = 253: compressed pixels
r_next:
    cmp  r0, #0
    beq  r_done
    mov  r5, r0
    b    r_loop
r_pixels:
    ldrb r2, [r5, #2]             @ w
    ldrb r3, [r5, #3]             @ h
    cmp  r2, #0
    beq  r_done
    cmp  r3, #0
    beq  r_done
    mov  r7, r2
    muls r7, r3, r7
    lsls r7, r7, #1               @ pixel bytes
    adds r0, r0, r7
    cmp  r0, r6
    bhi  r_done                   @ record would run past the block: ignore it
    ldrb r0, [r5, #0]             @ x
    ldrb r1, [r5, #1]             @ y
    bl   {SET_WINDOW:#x}
    adds r0, r5, #4
    mov  r1, r7
    bl   {LCD_DATA_BUF:#x}
    adds r5, #4
    adds r5, r5, r7
    b    r_loop
r_done:
    ldr  r1, ={LIVE_TICKS:#x}
    ldr  r0, ={LIVE_TIMEOUT}
    str  r0, [r1]                 @ live hold
    ldr  r1, ={LIVE_COUNT:#x}
    movs r0, #1
    strb r0, [r1]
    ldr  r0, ={BLOCK_NO:#x}
    ldrb r0, [r0]
    lsls r0, r0, #12              @ bytes received up to this block
    ldr  r1, ={TOTAL_LEN:#x}
    ldrb r2, [r1, #0]
    ldrb r3, [r1, #1]
    lsls r3, r3, #8
    orrs r2, r3                   @ declared upload length (16 bits)
    cmp  r0, r2
    bhs  w_last
    b    w_out
r_release:
    ldr  r1, ={PROGRESS_ACTIVE:#x}
    movs r0, #0
    strb r0, [r1, #0]             @ finish the upload
    strb r0, [r1, #1]
    ldr  r1, ={DISPLAY_ENABLED:#x}
    movs r0, #1
    strb r0, [r1]
    ldr  r1, ={LIVE_TICKS:#x}
    str  r0, [r1]                 @ hold ends on the next tick -> the page (clock) is repainted
    b    w_out
w_cols:
    ldr  r3, ={UPLOAD_REGION:#x}
    ldrb r6, [r3, #3]             @ h
    lsls r2, r6, #1               @ bytes per column
    movs r7, #0                   @ columns per block
    ldr  r0, =4096
    mov  r1, r2
w_cpb:
    cmp  r1, r0
    bhi  w_cpb_done
    adds r7, #1
    adds r1, r1, r2
    b    w_cpb
w_cpb_done:
    ldr  r0, ={BLOCK_NO:#x}
    ldrb r0, [r0]                 @ block number (1-based), low byte of the firmware's u32
    subs r0, #1
    muls r0, r7, r0               @ x0 = (block - 1) * cpb
    ldrb r5, [r3, #2]             @ region width
    cmp  r0, r5
    bhs  w_last                   @ only padding: nothing to draw
    subs r1, r5, r0               @ columns left in the region
    cmp  r1, r7
    bls  w_cw_ok
    mov  r1, r7
w_cw_ok:
    adds r5, r0, r1               @ r5 = x0 + cw (to test for the last block below)
    mov  r7, r1                   @ r7 = cw
    ldrb r2, [r3, #0]
    adds r0, r0, r2               @ x = left + x0
    ldrb r1, [r3, #1]             @ y = top
    mov  r2, r7                   @ w = cw
    mov  r3, r6                   @ h
    bl   {SET_WINDOW:#x}
    mov  r1, r7
    muls r1, r6, r1
    lsls r1, r1, #1               @ bytes = cw * h * 2 (<= 4096 by construction)
    ldr  r0, ={BLOCK_BUF:#x}
    bl   {LCD_DATA_BUF:#x}
    ldr  r1, ={LIVE_TICKS:#x}
    ldr  r0, ={LIVE_TIMEOUT}
    str  r0, [r1]                 @ live hold: page/clock drawing paused (every block)
    ldr  r1, ={LIVE_COUNT:#x}
    movs r0, #1
    strb r0, [r1]                 @ remember: a live block was drawn
    ldr  r3, ={UPLOAD_REGION:#x}
    ldrb r0, [r3, #2]
    cmp  r5, r0
    blo  w_out                    @ more blocks to come
w_last:
    @ Last block: finish the upload here (the CMK75 keyboard chip may send no commit, kind 0x24).
    ldr  r1, ={PROGRESS_ACTIVE:#x}
    movs r0, #0
    strb r0, [r1, #0]             @ 0x2007F progress screen active = 0
    strb r0, [r1, #1]             @ 0x20080 upload started = 0
    ldr  r1, ={DISPLAY_ENABLED:#x}
    movs r0, #1
    strb r0, [r1]                 @ 0x210C5 display enabled = 1
    ldr  r1, ={LIVE_TICKS:#x}
    ldr  r0, ={LIVE_TIMEOUT}
    str  r0, [r1]
w_out:
    pop  {{r5, r6, r7}}
    ldr  r0, =0x01014DC1          @ writer tail: record block number, set "block done", return
    bx   r0
w_normal:
    cmp  r0, #5
    bne  w_back
    pop  {{r1}}
    ldr  r1, =0x01014DFB          @ original "beq 0x01014DFA"
    bx   r1
w_back:
    pop  {{pc}}                   @ continue at 0x01014D3E
"""), ("data_hook", f"""
    @ P2. In: r0 = 0x20082, r1 = 0x200A3, r6 = 0x20080, r7 = block. lr held 0x210C0 (data register)
    push {{r0, r1, lr}}
    bl   {{is_live}}
    cmp  r0, #0
    pop  {{r0, r1}}
    beq  d_normal
    pop  {{r2}}                   @ drop return address
    ldr  r2, ={DATA_LR_VALUE:#x}
    mov  lr, r2                   @ the handler uses lr as a data register: restore it
    ldr  r2, =0x01012501          @ live: keep the current slot, just record the block number
    bx   r2
d_normal:
    ldrb r2, [r6, #0xa]
    strb r2, [r0, #4]
    pop  {{r3}}                   @ r3 is overwritten right after the return point
    ldr  r3, ={DATA_LR_VALUE:#x}
    mov  lr, r3
    ldr  r3, =0x010124F5
    bx   r3
"""), ("commit_hook", f"""
    @ P3. Inside the frame dispatcher; [sp] / [sp+4] are its locals
    push {{r0, r1, r2, lr}}
    bl   {{is_live}}
    cmp  r0, #0
    pop  {{r0, r1, r2}}
    beq  c_normal
    pop  {{r3}}                   @ drop return address
    @ Finish the upload exactly like the stock progress task does (state diffed in the emulator):
    ldr  r3, ={PROGRESS_ACTIVE:#x}
    movs r2, #0
    strb r2, [r3, #0]             @ 0x2007F progress screen active = 0
    strb r2, [r3, #1]             @ 0x20080 upload started = 0
    ldr  r3, ={DISPLAY_ENABLED:#x}
    movs r2, #1
    strb r2, [r3]                 @ 0x210C5 display enabled = 1 (pages redraw again)
    ldr  r3, ={LIVE_TICKS:#x}
    ldr  r2, ={LIVE_TIMEOUT}
    str  r2, [r3]                 @ live hold: pages stay paused for LIVE_TIMEOUT ticks
    movs r7, #0                   @ ack status = OK
    ldr  r3, =0x01012C45          @ build + send the normal "commit OK" ack, common exit
    bx   r3
c_normal:
    pop  {{r2}}                   @ return address (r2 is rewritten right after the return point)
    ldr  r6, [sp, #4]
    movs r3, #1
    bx   r2
""")]

# --- CMK75 fixes (2026-11-03, after the first flash) -------------------------------------------------------
# Kind 0x30 from the CMK75 keyboard chip = Fn + knob press "colour test" (keyboard fw: function 19 sub 3 sets the
# pending bit for a kind-0x30 frame). The ROYALAXE v108 handler treats 0x30 as "link down": it clears LINK_OK,
# fills the screen white and stops redrawing pages. We keep its reply but run v101's colour test instead.
# page byte, home page (timer target), current picture slot u32, per-slot "holds a picture" flags
PAGE, HOME, SLOT, SLOT_VALID = 0x00020053, 0x00020055, 0x00020086, 0x000200B5
# --pin38-high (hardware test 2026-11-03): pin 38 may be the CMK75 backlight enable (active high). v108 drives it
# low at LCD init and every tick while LINK_OK; only its "link lost" routine (white fill) drives it high.
# --pins56-high (hardware test 2026-11-03): pins 5/6 may switch the CMK75 backlight. v108's "link lost" routine
# 0x010122A0 (the white screen the user could see) drives 5, 6 and 38 high; nothing else ever drives 5/6 high
# unless the keyboard sends indicator bits. page_tick writes 0x3F (output high) to both every tick, and the
# indicator routine 0x01011E82 can no longer drive them low.
PINS56 = False
PINS56_CODE = """
    ldr  r1, =0x000F8700
    movs r0, #0x3F
    strb r0, [r1, #5]             @ pin 5 high
    strb r0, [r1, #6]             @ pin 6 high
"""
PINS56_TWEAKS = {
    0x01011EA6: ("strb r3, [r1, #6]", "strb r4, [r1, #6]"),
    0x01011EB2: ("strb r3, [r1, #5]", "strb r4, [r1, #5]"),
}
PIN38_HIGH = {
    0x01011156: ("movs r0, #0x3e", "movs r0, #0x3f"),    # LCD init: pin 38 high instead of low
    0x01011EF0: ("strb r3, [r0, #6]", "strb r4, [r0, #6]"),  # per-tick indicator routine: high instead of low
}
COLOR_SITE = 0x0101290C          # "strb r2, [r4, #6]; movs r0, #1" (then "strb r0, [r4, #7]" stays)
REDRAW = 0x000210D7              # page redraw request
LAST_BG = 0x000210F1             # page tasks clear the screen when this differs from the page's background id
CT_PERIOD = 700                  # ticks per colour
# main loop: bl 0x01011B94 (splash task; its tail draws the dashboard clock digits every tick),
#            bl 0x010117A2 (page display task), bl 0x010117FC (dashboard task)
PAGES_SITES = {0x010104D0: "bl #0x1011b94", 0x010104D4: "bl #0x10117a2", 0x010104D8: "bl #0x10117fc"}
SPLASH_TASK, PAGE_TASK, DASH_TASK = 0x01011B94, 0x010117A2, 0x010117FC
CLOCK_SITE, CLOCK_TASK = 0x010104E0, 0x01011FF6      # main loop: bl 0x01011FF6 (dashboard digit refresh)
# The CMK75 keyboard chip never sends the status frame (kind 0x22) that starts v108's splash and sets LINK_OK,
# so the screen stayed black until a key produced a screen message. After FORCE_LINK_TICKS we feed the frame
# task the same 0x22 frame ourselves, unless the keyboard already talked.
RX_SITE = 0x01012DB6             # frame task: "bl 0x01012866" (dispatcher)
DISPATCHER = 0x01012866
FRAME_BUF, FRAME_COUNT, FRAME_FLAG = 0x000214B8, 0x00020028, 0x00022570
# v18: the frame task (0x01012D4A) only accepts a buffer holding exactly ONE frame (count - 3 == len field). Two
# knob steps sent within the 2-tick idle gap land in the buffer together and BOTH were thrown away, while the PC
# counted them: fast turning put the screen and the PC on different cells (wrong action on press).
# rx_split replaces the frame task's final "bl buf_reset" and dispatches every complete, checksummed frame of a
# short merged buffer one by one before the reset.
RX_SPLIT_SITE = 0x01012DC2       # frame task: "bl 0x010122E8" (count = 0, buffer zeroed); reached on every path
BUF_RESET = 0x010122E8
RX_SPLIT_MAX = 128               # only short buffers (event messages); uploads (4 KB data frames) are untouched
FORCE_LINK_TICKS = 500
# Our RAM (0x27F00.., unused by the firmware; stack starts at 0x30000)
#   +0 u32 net state, +4 u32 net ticks, +8 u8 colour-test state, +12 u32 colour-test ticks,
#   +16 u8 boot-frame done, +17 u8 frame dispatched, +20 u32 boot ticks
CT_STATE, CT_COUNT = NET_STATE + 8, NET_STATE + 12
BOOT_DONE, RX_SEEN, BOOT_COUNT = NET_STATE + 16, NET_STATE + 17, NET_STATE + 20

COLOR_FUNCS = [("color_hook", f"""
    @ P6. Kind 0x30 in the dispatcher, r4 = 0x210E0. Leave LINK_OK alone, start the colour test.
    @ Must keep r1, r2 (= 0), r3; returns r0 = current [r4 + 7] for the following "strb r0, [r4, #7]".
    push {{r1, r2, r3, lr}}
    ldr  r1, ={CT_STATE:#x}
    movs r0, #1
    strb r0, [r1]
    movs r0, #0
    str  r0, [r1, #4]
    ldrb r0, [r4, #7]
    pop  {{r1, r2, r3, pc}}
"""), ("ct_tick", f"""
    @ Once per tick: red, green, blue, white (CT_PERIOD ticks each), then repaint the current page.
    push {{r4, lr}}
    sub  sp, #8
    ldr  r4, ={CT_STATE:#x}
    ldrb r0, [r4]
    cmp  r0, #0
    beq  ct_done
    cmp  r0, #5
    bhs  ct_end                   @ finished (or garbage)
    ldr  r1, [r4, #4]
    cmp  r1, #0
    bne  ct_count
    ldr  r2, =0xF800
    cmp  r0, #1
    beq  ct_draw
    ldr  r2, =0x07E0
    cmp  r0, #2
    beq  ct_draw
    movs r2, #0x1F
    cmp  r0, #3
    beq  ct_draw
    ldr  r2, =0xFFFF
ct_draw:
    str  r2, [sp]
    movs r0, #0
    movs r1, #0
    movs r2, #0xF0
    movs r3, #0x87
    bl   {FILL_RECT:#x}
ct_count:
    ldr  r1, [r4, #4]
    adds r1, #1
    str  r1, [r4, #4]
    ldr  r2, ={CT_PERIOD}
    cmp  r1, r2
    blo  ct_done
    movs r1, #0
    str  r1, [r4, #4]
    ldrb r0, [r4]
    adds r0, #1
    strb r0, [r4]
    cmp  r0, #5
    blo  ct_done
ct_end:
    movs r0, #0
    strb r0, [r4]
    ldr  r1, ={LAST_BG:#x}
    movs r0, #0xFF
    strb r0, [r1]                 @ make the page clear the screen
    ldr  r1, ={REDRAW:#x}
    movs r0, #1
    strb r0, [r1]                 @ and redraw
ct_done:
    add  sp, #8
    pop  {{r4, pc}}
"""), ("pages_gate", f"""
    @ P6b. Replaces the main loop's three drawing calls (splash + clock digits, page display, dashboard) so
    @ nothing paints over the colour test. The 2nd and 3rd call sites become "mov r8, r8" pairs.
    push {{r4, lr}}
    ldr  r0, ={CT_STATE:#x}
    ldrb r0, [r0]
    cmp  r0, #0
    bne  pg_skip
    ldr  r0, ={LIVE_TICKS:#x}
    ldr  r0, [r0]
    cmp  r0, #0
    bne  pg_skip                  @ live hold
    bl   {SPLASH_TASK:#x}
    bl   {PAGE_TASK:#x}
    bl   {DASH_TASK:#x}
pg_skip:
    pop  {{r4, pc}}
"""), ("clock_gate", f"""
    @ P6c. Replaces the main loop's "bl 0x01011FF6" (clock dashboard digit refresh): skipped during the test.
    ldr  r0, ={CT_STATE:#x}
    ldrb r0, [r0]
    cmp  r0, #0
    bne  cg_skip
    ldr  r0, ={LIVE_TICKS:#x}
    ldr  r0, [r0]
    cmp  r0, #0
    bne  cg_skip                  @ live hold
    ldr  r0, ={CLOCK_TASK | 1:#x}
    bx   r0                       @ tail call, lr = main loop
cg_skip:
    bx   lr
"""), ("page_tick", f"""
    @ Once per tick. (1) Home page = the last "resting" page: the clock (0) or a picture page (4) whose slot
    @ holds a valid picture. v108's page timer (forced into its "return to home" mode, see CMK75_TWEAKS)
    @ brings pop-up pages back there after 3500 ticks. (2) Live hold countdown; at 0 repaint the page.
    ldr  r1, ={PAGE:#x}
    ldrb r0, [r1]
    cmp  r0, #0
    beq  pt_home
    cmp  r0, #4
    bne  pt_live
    ldr  r2, ={SLOT:#x}
    ldrb r2, [r2]                 @ BYTE load: 0x20086 is not word aligned (Cortex-M0 faults on ldr there)
    cmp  r2, #6
    bhs  pt_live
    ldr  r3, ={SLOT_VALID:#x}
    ldrb r3, [r3, r2]
    cmp  r3, #0
    beq  pt_live                  @ empty slot: do not make it home
pt_home:
    ldr  r2, ={HOME:#x}
    strb r0, [r2]
pt_live:
    ldr  r1, ={LIVE_TICKS:#x}
    ldr  r0, [r1]
    cmp  r0, #0
    beq  pt_done
    subs r0, #1
    str  r0, [r1]
    bne  pt_done
    ldr  r1, ={LAST_BG:#x}
    movs r0, #0xFF
    strb r0, [r1]                 @ hold over: clear the screen and redraw the page
    ldr  r1, ={REDRAW:#x}
    movs r0, #1
    strb r0, [r1]
pt_done:
@PINS56@
    bx   lr
"""), ("rx_hook", f"""
    @ P7a. Replaces the frame task's "bl dispatcher": note that a frame got dispatched, then tail-call it.
    ldr  r3, ={RX_SEEN:#x}
    movs r2, #1
    strb r2, [r3]
    ldr  r3, ={DISPATCHER | 1:#x}
    bx   r3
"""), ("boot_tick", f"""
    @ P7b. Once per tick until done: after FORCE_LINK_TICKS, if the keyboard chip has not talked yet, put a
    @ kind-0x22 status frame (8 zero bytes) in the UART buffer exactly like the receive interrupt does.
    ldr  r1, ={BOOT_DONE:#x}
    ldrb r0, [r1]
    cmp  r0, #0
    bne  bt_done
    ldr  r0, [r1, #4]
    adds r0, #1
    str  r0, [r1, #4]
    ldr  r2, ={FORCE_LINK_TICKS}
    cmp  r0, r2
    blo  bt_done
    movs r0, #1
    strb r0, [r1]                 @ only once
    ldr  r2, ={LINK_OK:#x}
    ldrb r0, [r2]
    cmp  r0, #0
    bne  bt_done
    ldr  r2, ={FRAME_FLAG:#x}
    ldrb r0, [r2]
    cmp  r0, #0
    bne  bt_done                  @ a real frame is arriving right now: leave the buffer alone
    ldr  r1, ={FRAME_BUF:#x}
    movs r0, #0x55
    strb r0, [r1, #0]
    movs r0, #0x0A
    strb r0, [r1, #1]             @ length = total - 3 = 10
    movs r0, #0
    strb r0, [r1, #2]
    movs r0, #0x22
    strb r0, [r1, #3]             @ kind 0x22
    movs r0, #0
    movs r3, #4
bt_zero:
    strb r0, [r1, r3]
    adds r3, #1
    cmp  r3, #12
    blo  bt_zero
    movs r0, #0x2C
    strb r0, [r1, #12]            @ checksum = 0x0A + 0x22
    ldr  r1, ={FRAME_COUNT:#x}
    movs r0, #13
    str  r0, [r1]
    movs r0, #1
    strb r0, [r2]                 @ "frame received" (the receive interrupt writes 1, then 0 at +1)
    movs r0, #0
    strb r0, [r2, #1]
bt_done:
    bx   lr
""")]


DEBUG = False                    # --debug-squares: status squares top-left (hardware diagnosis)
DEBUG_FUNCS = [("dbg_tick", f"""
    @ Every 50 ticks draw five 20x12 squares at the top left: green = yes, red = no.
    @ 1 live hold active, 2 a live block was drawn since boot, 3 display enabled, 4 header counts as live,
    @ 5 upload in progress.
    push {{r4, r5, r6, lr}}
    ldr  r4, ={DBG_COUNT:#x}
    ldrb r0, [r4]
    adds r0, #1
    strb r0, [r4]
    cmp  r0, #50
    blo  d_done
    movs r0, #0
    strb r0, [r4]
    movs r5, #0
    ldr  r1, ={LIVE_TICKS:#x}
    ldr  r0, [r1]
    bl   d_sq
    movs r5, #24
    ldr  r1, ={LIVE_COUNT:#x}
    ldrb r0, [r1]
    bl   d_sq
    movs r5, #48
    ldr  r1, ={DISPLAY_ENABLED:#x}
    ldrb r0, [r1]
    bl   d_sq
    movs r5, #72
    bl   {{is_live}}
    bl   d_sq
    movs r5, #96
    ldr  r1, =0x00020080
    ldrb r0, [r1]
    bl   d_sq
d_done:
    pop  {{r4, r5, r6, pc}}
d_sq:
    @ r0 = condition, r5 = x
    push {{r4, lr}}
    sub  sp, #8
    ldr  r2, =0x07E0
    cmp  r0, #0
    bne  d_c
    ldr  r2, =0xF800
d_c:
    str  r2, [sp]
    mov  r0, r5
    movs r1, #0
    movs r2, #20
    movs r3, #12
    bl   {FILL_RECT:#x}
    add  sp, #8
    pop  {{r4, pc}}
""")]


# --- Software clock (CMK75) ------------------------------------------------------------------------------
# v108 keeps time in a PCF8563-type RTC (I2C 0xA2/0xA3, bit-banged; write 0x01015222(reg, bcd), read
# 0x01015250(reg) -> bcd). The CMK75 board returns garbage there (date drifting 2100 -> 2200). Both routines are
# redirected to SWRTC in RAM (same register numbers: 2 sec, 3 min, 4 hour, 5 day, 7 month, 8 year), advanced
# once per 1000 ms of the timer ISR's millisecond counter. The CMK75 keyboard chip forwards the PC's clock
# command (0x28) as kind 1 [page, sub, year lo, year hi, month, day, hour, min, sec] with ITS page/sub, which
# v108 ignored; kind1_hook recognises it by its length and sets SWRTC (and the full year at 0x210DD).
RTC_WRITE, RTC_READ = 0x01015222, 0x01015250
RX_SPLIT_FUNCS = [("rx_split", f"""
    @ v18. Called instead of buf_reset at the end of the frame task. A buffer that held exactly one frame was
    @ already dispatched by the stock code; otherwise walk it and dispatch each complete frame with a good checksum
    @ (moved to the buffer start, count = its length, like a frame that arrived alone), then reset as before.
    push {{r4, r5, r6, r7, lr}}
    ldr  r6, ={FRAME_BUF:#x}
    ldr  r5, ={FRAME_COUNT:#x}
    ldr  r7, [r5]                 @ bytes received
    ldrb r0, [r6, #1]
    ldrb r1, [r6, #2]
    lsls r1, r1, #8
    orrs r0, r1                   @ length field of the first frame
    subs r1, r7, #3
    cmp  r1, r0
    beq  rs_done                  @ exactly one frame: the stock code handled it
    movs r1, #{RX_SPLIT_MAX}
    cmp  r7, r1
    bhi  rs_done                  @ long buffer: not ours to split
    movs r4, #0                   @ offset of the next frame
rs_loop:
    adds r0, r4, #5
    cmp  r0, r7
    bhi  rs_done                  @ fewer than 5 bytes left
    ldrb r0, [r6, r4]
    cmp  r0, #0x55
    bne  rs_done                  @ lost the framing: stop
    adds r1, r6, r4               @ r1 = this frame
    ldrb r0, [r1, #1]
    ldrb r2, [r1, #2]
    lsls r2, r2, #8
    orrs r0, r2
    adds r0, #3                   @ r0 = total length
    adds r2, r4, r0
    cmp  r2, r7
    bhi  rs_done                  @ incomplete frame
    subs r0, #1                   @ index of the checksum byte
    movs r2, #0
    movs r3, #1
rs_sum:
    cmp  r3, r0
    bhs  rs_sum_end
    ldrb r5, [r1, r3]
    adds r2, r2, r5
    adds r3, #1
    b    rs_sum
rs_sum_end:
    uxtb r2, r2
    ldrb r5, [r1, r0]
    adds r0, #1                   @ total length again
    cmp  r5, r2
    bne  rs_next                  @ bad checksum: skip this frame
    movs r3, #0
rs_copy:                          @ to the buffer start (forward copy: destination <= source, later frames untouched)
    ldrb r5, [r1, r3]
    strb r5, [r6, r3]
    adds r3, #1
    cmp  r3, r0
    blo  rs_copy
    push {{r0}}
    ldr  r5, ={FRAME_COUNT:#x}
    str  r0, [r5]                 @ the dispatcher sees this frame alone
    ldr  r5, ={FRAME_FLAG:#x}
    movs r2, #0
    strb r2, [r5, #6]             @ as the frame task does before dispatching
    bl   {{rx_hook}}
    ldr  r5, ={FRAME_FLAG:#x}
    ldrb r0, [r5, #6]
    cmp  r0, #0
    beq  rs_disp_done
    movs r0, #1
    strb r0, [r5, #7]             @ as the frame task does after dispatching
rs_disp_done:
    pop  {{r0}}
rs_next:
    adds r4, r4, r0
    b    rs_loop
rs_done:
    bl   {BUF_RESET:#x}
    pop  {{r4, r5, r6, r7, pc}}
""")]

MS_COUNTER = 0x00020048          # u32, incremented by the timer ISR every tick (1 ms: reload 96000 at 96 MHz)
KIND1_SITE = 0x010128EE          # dispatcher kind 1: "bl 0x0101282A"
KIND1_HANDLER = 0x0101282A
YEAR_FULL = 0x000210DD           # u32 (byte-packed) full year; the ISR keeps its century + RTC year register
BIN2BCD = 0x01015298
CLOCK_FUNCS = [("sw_rtc_write", f"""
    @ r0 = register, r1 = BCD value
    cmp  r0, #9
    bhs  swr_x
    ldr  r2, ={SWRTC:#x}
    strb r1, [r2, r0]
swr_x:
    bx   lr
"""), ("sw_rtc_read", f"""
    @ r0 = register -> r0 = BCD value
    cmp  r0, #9
    bhs  swr_zero
    ldr  r2, ={SWRTC:#x}
    ldrb r0, [r2, r0]
    bx   lr
swr_zero:
    movs r0, #0
    bx   lr
"""), ("sw_tick", f"""
    @ Once per main-loop tick: advance SWRTC by one second per 1000 ms of the ISR counter.
    push {{r4, r5, r6, lr}}
    ldr  r1, ={MS_COUNTER:#x}
    ldr  r0, [r1]
    ldr  r4, ={SW_LAST:#x}
    ldr  r1, [r4]
    subs r0, r0, r1
    ldr  r2, =1000
    cmp  r0, r2
    blo  st_done
    adds r1, r1, r2
    str  r1, [r4]                 @ one second consumed (catches up one per tick if behind)
    ldr  r5, ={SWRTC:#x}
    ldrb r0, [r5, #2]
    movs r1, #0x59
    movs r2, #0
    bl   bcd_step
    strb r0, [r5, #2]             @ seconds
    cmp  r1, #0
    beq  st_done
    ldrb r0, [r5, #3]
    movs r1, #0x59
    movs r2, #0
    bl   bcd_step
    strb r0, [r5, #3]             @ minutes
    cmp  r1, #0
    beq  st_done
    ldrb r0, [r5, #4]
    movs r1, #0x23
    movs r2, #0
    bl   bcd_step
    strb r0, [r5, #4]             @ hours
    cmp  r1, #0
    beq  st_done
    @ last day of the month (BCD) -> r6
    movs r6, #0x31
    ldrb r0, [r5, #7]             @ month (BCD)
    cmp  r0, #0x02
    beq  st_feb
    cmp  r0, #0x04
    beq  st_30
    cmp  r0, #0x06
    beq  st_30
    cmp  r0, #0x09
    beq  st_30
    cmp  r0, #0x11
    beq  st_30
    b    st_day
st_30:
    movs r6, #0x30
    b    st_day
st_feb:
    movs r6, #0x28
    ldrb r0, [r5, #8]             @ year (BCD): leap if the binary value is a multiple of 4
    lsrs r1, r0, #4
    movs r2, #10
    muls r1, r2, r1
    movs r2, #0xF
    ands r0, r2
    adds r0, r0, r1
    movs r2, #3
    tst  r0, r2
    bne  st_day
    movs r6, #0x29
st_day:
    ldrb r0, [r5, #5]
    mov  r1, r6
    movs r2, #1
    bl   bcd_step
    strb r0, [r5, #5]             @ day
    cmp  r1, #0
    beq  st_done
    ldrb r0, [r5, #7]
    movs r1, #0x12
    movs r2, #1
    bl   bcd_step
    strb r0, [r5, #7]             @ month
    cmp  r1, #0
    beq  st_done
    ldrb r0, [r5, #8]
    movs r1, #0x99
    movs r2, #0
    bl   bcd_step
    strb r0, [r5, #8]             @ year
st_done:
    pop  {{r4, r5, r6, pc}}
bcd_step:
    @ r0 = BCD value, r1 = BCD maximum, r2 = value after wrapping -> r0 = next, r1 = 1 if it wrapped
    cmp  r0, r1
    bhs  bs_wrap
    movs r3, #0xF
    ands r3, r0
    cmp  r3, #9
    bhs  bs_tens
    adds r0, #1
    movs r1, #0
    bx   lr
bs_tens:
    adds r0, #7                   @ x9 -> (x+1)0
    movs r1, #0
    bx   lr
bs_wrap:
    mov  r0, r2
    movs r1, #1
    bx   lr
"""), ("kind1_hook", f"""
    @ Replaces the dispatcher's "bl 0x0101282A" for kind 1 (r1 = payload). A 9-byte payload (frame length
    @ field 11) is the CMK75 keyboard chip's clock message; everything else goes to the stock handler.
    ldr  r2, ={FRAME_BUF + 1:#x}
    ldrb r2, [r2]
    cmp  r2, #11
    beq  k1_time
    cmp  r2, #5
    bne  k1_orig
    @ [page, sub, event]: the keyboard chip's event message. Knob turns = volume up 0x0E / down 0x0F.
    ldrb r2, [r1, #2]
    cmp  r2, #0x06
    beq  k1_press
    movs r3, #1
    cmp  r2, #0x0E
    beq  k1_nav
    subs r3, #2                   @ -1
    cmp  r2, #0x0F
    bne  k1_orig
k1_nav:
    ldr  r2, ={NAV:#x}
    ldrb r0, [r2, #0]
    cmp  r0, #0
    beq  k1_orig                  @ frame moves off (volume mode)
    ldrb r0, [r2, #10]
    adds r0, r0, r3
    strb r0, [r2, #10]            @ drawn by nav_tick in the main loop
    b    k1_orig
k1_press:
    ldr  r2, ={NAV:#x}
    ldrb r0, [r2, #0]
    cmp  r0, #0
    beq  k1_orig
    ldr  r2, ={FLASH_ON:#x}
    movs r0, #1
    strb r0, [r2]                 @ press feedback, drawn by nav_tick
k1_orig:
    ldr  r2, ={KIND1_HANDLER | 1:#x}
    bx   r2                       @ tail call, lr = dispatcher
k1_time:
    push {{r4, r5, r6, lr}}
    mov  r4, r1
    ldr  r5, ={SWRTC:#x}
    ldrb r0, [r4, #8]
    bl   {BIN2BCD:#x}
    strb r0, [r5, #2]             @ seconds
    ldrb r0, [r4, #7]
    bl   {BIN2BCD:#x}
    strb r0, [r5, #3]             @ minutes
    ldrb r0, [r4, #6]
    bl   {BIN2BCD:#x}
    strb r0, [r5, #4]             @ hours
    ldrb r0, [r4, #5]
    bl   {BIN2BCD:#x}
    strb r0, [r5, #5]             @ day
    ldrb r0, [r4, #4]
    bl   {BIN2BCD:#x}
    strb r0, [r5, #7]             @ month
    ldrb r0, [r4, #2]
    ldrb r1, [r4, #3]
    lsls r1, r1, #8
    orrs r0, r1                   @ full year
    ldr  r2, ={YEAR_FULL:#x}
    strb r0, [r2, #0]
    lsrs r1, r0, #8
    strb r1, [r2, #1]
    movs r1, #0
    strb r1, [r2, #2]
    strb r1, [r2, #3]
k1_mod:
    cmp  r0, #100
    blo  k1_mod_done
    subs r0, #100
    b    k1_mod
k1_mod_done:
    bl   {BIN2BCD:#x}
    strb r0, [r5, #8]             @ year (2 digits)
    ldr  r1, ={MS_COUNTER:#x}
    ldr  r0, [r1]
    ldr  r1, ={SW_LAST:#x}
    str  r0, [r1]                 @ the second starts now
    pop  {{r4, r5, r6, pc}}       @ back into the dispatcher (ack + LINK_OK as for any kind-1 message)
""")]


BOOT_FRAME = False               # --boot-frame: add the self-start (not needed on the CMK75; suspect in v3 failure)


def late_funcs(net: bool):
    """boot_hook and progress_gate, with or without the safety net."""
    boot_call = "    bl   {boot_tick}\n" if BOOT_FRAME else ""
    dbg_call = "    bl   {dbg_tick}\n" if DEBUG else ""
    arm = f"""
    sub  sp, #32
    mov  r0, sp
    movs r1, #0xff
    movs r2, #0
b_fill:
    strb r1, [r0, r2]
    adds r2, #1
    cmp  r2, #32
    blo  b_fill
    movs r1, #0xaa
    strb r1, [r0, #0]
    strb r1, [r0, #2]
    movs r1, #0x55
    strb r1, [r0, #1]
    strb r1, [r0, #3]             @ AA 55 AA 55 + 28 x FF, exactly what the vendor writes
    ldr  r0, ={FLAG_ADDR:#x}
    bl   {INT_ERASE:#x}
    ldr  r0, ={FLAG_ADDR:#x}
    mov  r1, sp
    movs r2, #32
    bl   {INT_PROGRAM:#x}
    add  sp, #32
    ldr  r0, ={NET_STATE:#x}
    movs r1, #1
    str  r1, [r0]                 @ armed
""" if net else ""
    boot = f"""
    @ P5 (+ init of our RAM). Replaces main's "bl 0x01010458" right after the clock setup.
    push {{r4, lr}}
    ldr  r0, ={NET_STATE:#x}
    movs r1, #0
    movs r2, #0
b_zero:
    str  r1, [r0, r2]             @ clear our state block
    adds r2, #4
    cmp  r2, #{STATE_BYTES}
    blo  b_zero
    movs r1, #1
    ldr  r0, ={SWRTC:#x}
    strb r1, [r0, #5]             @ software clock starts at day 1
    strb r1, [r0, #7]             @ month 1
{arm}
    bl   {NET_HOOK_TARGET:#x}     @ the original init call
    pop  {{r4, pc}}
"""
    disarm = f"""
    ldr  r1, ={NET_STATE:#x}
    ldr  r0, [r1]
    cmp  r0, #1
    bne  g_net_done
    ldr  r0, [r1, #4]
    adds r0, #1
    str  r0, [r1, #4]
    ldr  r2, ={DISARM_TICKS}
    cmp  r0, r2
    blo  g_net_done
    ldr  r2, ={RX_SEEN:#x}
    ldrb r2, [r2]
    cmp  r2, #0
    beq  g_net_done               @ the frame task has not dispatched a single frame yet
    movs r0, #2
    str  r0, [r1]                 @ disarmed (only once)
    ldr  r0, ={FLAG_ADDR:#x}
    bl   {INT_ERASE:#x}           @ flag back to FF: normal boots from now on
g_net_done:
""" if net else ""
    gate = f"""
    @ P4 + P5 disarm + per-tick work. Replaces the main loop's call to the progress-screen task.
    push {{r4, lr}}
    bl   {{ct_tick}}
{boot_call}    bl   {{page_tick}}
    bl   {{sw_tick}}
    bl   {{nav_tick}}
{dbg_call}
{disarm}
    pop  {{r4}}
    pop  {{r0}}
    mov  lr, r0
    @ P4: skip the progress screen during live uploads. Uses only the header marker,
    @ because the task can run after the header but before any data frame arrives.
    ldr  r0, ={UPLOAD_DELAY:#x}
    ldrb r0, [r0]
    cmp  r0, #{LIVE_DELAY:#x}
    bne  g_run
    ldr  r0, ={UPLOAD_FRAMES:#x}
    ldrb r0, [r0]
    cmp  r0, #1
    beq  g_skip
g_run:
    ldr  r0, ={PROGRESS_TASK | 1:#x}
    bx   r0                       @ tail call: the task returns straight to the main loop (lr intact)
g_skip:
    bx   lr
"""
    return [("boot_hook", boot), ("progress_gate", gate)]


def asm(src, addr):
    ks = Ks(KS_ARCH_ARM, KS_MODE_THUMB)
    enc, _ = ks.asm(src, addr=addr)
    code = bytes(enc)
    check_m0(code, addr)
    return code


def check_m0(code: bytes, addr: int) -> None:
    """The screen chip is a Cortex-M0 (ARMv6-M): the only 32-bit instruction it has is BL. Keystone silently
    emits Thumb-2 when an operand does not fit (adds r2, r4, #11 -> adds.w; far beq -> beq.w), and Unicorn runs
    it, so the emulator cannot catch it: refuse to build. Literal-pool words (targets of ldr rX, [pc, #n]) are
    data, not code. Also rejects the 16-bit Thumb-2 instructions ARMv6-M lacks (cbz, cbnz, it)."""
    import struct as _struct
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    md.skipdata = True
    insns = list(md.disasm(code, addr))
    pool = set()
    for i in insns:
        if i.mnemonic == "ldr" and "[pc, #" in i.op_str:
            la = ((i.address + 4) & ~3) + int(i.op_str.split("#")[1].rstrip("]"), 0)
            pool.update(range(la, la + 4))
    for i in insns:
        if i.address in pool or i.mnemonic == ".byte":
            continue
        if (i.size == 4 and i.mnemonic != "bl") or i.mnemonic in ("cbz", "cbnz") or i.mnemonic.startswith("it"):
            sys.exit(f"not a Cortex-M0 instruction at 0x{i.address:08X}: {i.mnemonic} {i.op_str} ({i.bytes.hex()}); "
                     "use a 16-bit form (e.g. a register operand or a smaller immediate)")


def assemble_all(base, net=True):
    """Assemble each function at its own address (literal pool right after it), in order."""
    syms, code, addr = {}, b"", base
    funcs = {n: f for n, f in NAV_FUNCS + MENU_FUNCS}
    order = ["nav_frame", "nav_redraw", "rec_packbits", "draw_records", "show_page", "nav_move", "nav_tick",
             "rec_layout", "rec_flash", "rec_show"]
    assert set(order) == set(funcs), set(funcs) ^ set(order)
    for name, src in [(n, funcs[n]) for n in order] + PATCH_FUNCS + COLOR_FUNCS + RX_SPLIT_FUNCS + CLOCK_FUNCS + (DEBUG_FUNCS if DEBUG else []) + late_funcs(net):
        syms[name] = addr
        text = src.replace("@PINS56@", PINS56_CODE if PINS56 else "")
        for k, v in syms.items():
            text = text.replace("{" + k + "}", f"{v:#x}")
        part = asm(text, addr)
        part += b"\x00" * (-len(part) % 4)          # keep every function 4-byte aligned
        code += part
        addr += len(part)
    return syms, code




SPLASH_ADDR, SPLASH_W, SPLASH_H = 0x01015A68, 220, 24   # 1-bit bitmap, column-major, 3 bytes/column, MSB = top

# CMK75 mounts the panel rotated 180 deg compared with the ROYALAXE boards (seen on the real keyboard):
# MADCTL 0x80 (MY) -> 0x40 (MX), and the column offset 52 -> 53 (135-px panel in the 240-col GRAM).
ROTATE_180 = {
    0x01010858: ("movs r0, #0x80", "movs r0, #0x40"),    # MADCTL value in the LCD init
    0x0101072A: ("adds r0, #0x34", "adds r0, #0x35"),    # set_window: column start
    0x01010734: ("adds r0, #0x33", "adds r0, #0x34"),    # set_window: column end
    0x01010D24: ("adds r0, #0x34", "adds r0, #0x35"),    # draw-from-flash: column start
    0x01010D2E: ("adds r0, #0x33", "adds r0, #0x34"),    # draw-from-flash: column end
}
# After the splash v108 shows page 1 (ROYALAXE Bluetooth status, "not connected" in Chinese on the CMK75).
# Page 0 is the clock dashboard (date, battery, time; task 0x010117FC): store r4 (= 0) as the page instead of r1.
CMK75_TWEAKS = {
    0x01011BEA: ("strb r1, [r6, #0x13]", "strb r4, [r6, #0x13]"),
    # Page timer 0x01011D94: in mode 0 (default) it returns to page 1 (Bluetooth status) 3500 ticks after any
    # other page. Always take its mode-1 branch instead: return to the home page 0x20055 (kept by page_tick).
    0x01011DAE: ("cmp r2, #1", "cmp r2, r2"),
    # Kind-1 page-4 handler 0x01012510 requests a settings save (flash sector 0 erase + program) on EVERY
    # message; CMK75 media keys / knob turns send kind-1 messages, so that would wear the flash. Drop the request.
    0x01012526: ("strb r0, [r1, #0xe]", "mov r8, r8"),
}


def splash_bitmap(text: str | None = None, image_path: str | None = None) -> bytes:
    """Render the connect-splash bitmap (drawn with the firmware's rainbow animation)."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("1", (SPLASH_W, SPLASH_H), 0)
    if image_path:
        img = Image.open(image_path).convert("L").resize((SPLASH_W, SPLASH_H)).point(lambda v: 255 if v > 127 else 0, "1")
    elif text:
        d = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("arialbd.ttf", 20)
        except OSError:
            font = ImageFont.load_default()
        chars = list(text.upper())
        slot = SPLASH_W / len(chars)                 # spread letters evenly, like the original
        for i, ch in enumerate(chars):
            d.text((slot * (i + 0.5), SPLASH_H / 2), ch, font=font, fill=1, anchor="mm")
    out = bytearray()
    for x in range(SPLASH_W):
        col = 0
        for y in range(SPLASH_H):
            col = (col << 1) | (1 if img.getpixel((x, y)) else 0)
        out += col.to_bytes(3, "big")
    return bytes(out)


def disasm(code, addr, n=2):
    return [f"{i.mnemonic} {i.op_str}".strip() for i in Cs(CS_ARCH_ARM, CS_MODE_THUMB).disasm(code, addr)][:n]


def main():
    src_path, out_path = Path(sys.argv[1]), Path(sys.argv[2])
    net = "--no-net" not in sys.argv[3:]
    global BOOT_FRAME
    BOOT_FRAME = "--boot-frame" in sys.argv[3:]      # off by default: v3 with it never disarmed on the CMK75
    print("self-start (boot frame):", "ON" if BOOT_FRAME else "OFF")
    args = sys.argv[3:]
    splash_text = args[args.index("--splash") + 1] if "--splash" in args else None
    splash_img = args[args.index("--splash-image") + 1] if "--splash-image" in args else None
    fw = bytearray(src_path.read_bytes())
    if "--royalaxe-orientation" not in args:
        for site, (old, new) in ROTATE_180.items():
            off = site - IMAGE_BASE
            found = disasm(bytes(fw[off:off + 2]), site, n=1)
            if found != [old]:
                sys.exit(f"refusing to patch orientation: 0x{site:08X} holds {found}, expected {old}")
            fw[off:off + 2] = asm(new, site)
        print("orientation: rotated 180 deg for the CMK75 panel")
    for site, (old, new) in CMK75_TWEAKS.items():
        off = site - IMAGE_BASE
        found = disasm(bytes(fw[off:off + 2]), site, n=1)
        if found != [old]:
            sys.exit(f"refusing to patch: 0x{site:08X} holds {found}, expected {old}")
        fw[off:off + 2] = asm(new, site)
        assert len(asm(new, site)) == 2
    print("post-splash page: clock dashboard (page 0)")
    global DEBUG
    DEBUG = "--debug-squares" in args
    print("debug squares:", "ON" if DEBUG else "OFF")
    if "--no-pins56" not in args:                   # CMK75 backlight (confirmed on hardware)
        global PINS56
        PINS56 = True
        for site, (old, new) in PINS56_TWEAKS.items():
            off = site - IMAGE_BASE
            found = disasm(bytes(fw[off:off + 2]), site, n=1)
            if found != [old]:
                sys.exit(f"refusing to patch: 0x{site:08X} holds {found}, expected {old}")
            fw[off:off + 2] = asm(new, site)
        print("pins 5 and 6 held high (CMK75 backlight)")
    if "--pin38-high" in args:
        for site, (old, new) in PIN38_HIGH.items():
            off = site - IMAGE_BASE
            found = disasm(bytes(fw[off:off + 2]), site, n=1)
            if found != [old]:
                sys.exit(f"refusing to patch: 0x{site:08X} holds {found}, expected {old}")
            fw[off:off + 2] = asm(new, site)
        print("pin 38 held high (backlight test)")
    if splash_text is not None or splash_img:
        off = SPLASH_ADDR - IMAGE_BASE
        bmp = splash_bitmap(splash_text, splash_img)
        assert len(bmp) == SPLASH_W * SPLASH_H // 8
        fw[off:off + len(bmp)] = bmp
        print(f"splash replaced with {splash_text!r}" if splash_text is not None else f"splash image {splash_img}")
    end = IMAGE_BASE + len(fw)
    base = (end + 0xF) & ~0xF
    syms, code = assemble_all(base, net=net)
    print(f"image ends 0x{end:08X}; patch code at 0x{base:08X}, {len(code)} bytes; safety net {'ON' if net else 'OFF'}")
    for k in syms:
        print(f"   {k:<14} 0x{syms[k]:08X}")
    targets = {0x01014D3A: syms["writer_hook"], 0x010124F0: syms["data_hook"], 0x01012AF8: syms["commit_hook"],
               0x010104C4: syms["progress_gate"]}
    hooks = dict(HOOKS)
    hooks[NET_HOOK] = ["bl #0x1010458"]                       # always: it also clears our RAM state
    targets[NET_HOOK] = syms["boot_hook"]
    hooks[COLOR_SITE] = ["strb r2, [r4, #6]", "movs r0, #1"]  # P6 kind 0x30 -> colour test
    targets[COLOR_SITE] = syms["color_hook"]
    hooks[RX_SITE] = ["bl #0x1012866"]                        # P7a frame dispatched
    targets[RX_SITE] = syms["rx_hook"]
    hooks[RX_SPLIT_SITE] = [f"bl #{BUF_RESET:#x}"]            # v18 merged frames (fast knob turns)
    targets[RX_SPLIT_SITE] = syms["rx_split"]
    hooks[KIND1_SITE] = ["bl #0x101282a"]                    # P8 clock message from the CMK75 keyboard
    targets[KIND1_SITE] = syms["kind1_hook"]
    for site, fn, pad in ((RTC_WRITE, "sw_rtc_write", 1), (RTC_READ, "sw_rtc_read", 2)):
        off = site - IMAGE_BASE
        if disasm(bytes(fw[off:off + 2]), site, n=1)[0].split()[0] != "push":
            sys.exit(f"refusing to patch: 0x{site:08X} is not a function entry")
        stub = bytes.fromhex("014b") + bytes.fromhex("1847") + bytes.fromhex("c046") * pad
        lit = (site + 4 & ~3) + 4                     # ldr r3, [pc, #4]
        assert site + len(stub) == lit, (hex(site), len(stub))
        fw[off:off + len(stub) + 4] = stub + (syms[fn] | 1).to_bytes(4, "little")
        print(f"   0x{site:08X}: RTC routine -> {fn} 0x{syms[fn]:08X}")
    hooks[CLOCK_SITE] = ["bl #0x1011ff6"]                     # P6c clock digit refresh, skipped during the test
    targets[CLOCK_SITE] = syms["clock_gate"]
    first, *rest = PAGES_SITES                                # P6b drawing tasks, skipped during the test
    hooks[first] = [PAGES_SITES[first]]
    targets[first] = syms["pages_gate"]
    for site in rest:
        off = site - IMAGE_BASE
        if disasm(bytes(fw[off:off + 4]), site, n=1) != [PAGES_SITES[site]]:
            sys.exit(f"refusing to patch: 0x{site:08X} is not {PAGES_SITES[site]}")
        fw[off:off + 4] = asm("mov r8, r8\nmov r8, r8", site)  # now called from pages_gate
        print(f"   0x{site:08X}: {PAGES_SITES[site]} -> 2x mov r8, r8 (called from pages_gate)")
    for site, expected in hooks.items():
        off = site - IMAGE_BASE
        found = disasm(bytes(fw[off:off + 4]), site, n=len(expected))
        if found != expected:
            sys.exit(f"refusing to patch: 0x{site:08X} holds {found}, expected {expected}")
        bl = asm(f"bl {targets[site]:#x}", site)
        assert len(bl) == 4
        fw[off:off + 4] = bl
        print(f"   hook 0x{site:08X}: {expected} -> bl 0x{targets[site]:08X}")
    fw += b"\xff" * (base - end) + code
    out_path.write_bytes(bytes(fw))
    print(f"wrote {out_path} ({len(fw)} bytes, +{len(fw) - len(src_path.read_bytes())})")


if __name__ == "__main__":
    main()
