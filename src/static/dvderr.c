#include "dvderr.h"

#include "libultra/libultra.h"
#include "dolphin/dvd.h"
#include "libforest/gbi_extensions.h"
#include "libforest/emu64/emu64_wrapper.h"
#include "jsyswrap.h"
#include "dolphin/vi.h"
#include "dolphin/gx.h"

#ifdef PC_LOW_ADDRESS_64
#include "../src/data/pc_split/dvderr_split.h"
#else
#include "../src/static/dvderr_gfx.c_inc"
#endif

static dvderr_work* const this = &Dvderr_work;



static void dvderr_exec_dl(Gfx* dl) {
  emu64_init();
  emu64_taskstart(dl);
  emu64_cleanup();
}

static int dvderr_check_drive() {
  int err = DVDERR_NONE;

  if (osShutdown == FALSE) {
    switch (DVDGetDriveStatus()) {
      case DVD_STATE_FATAL_ERROR:
        err = DVDERR_FATAL;
        break;

      case DVD_STATE_BUSY:
        err = this->next_error;
        break;

      case DVD_STATE_NO_DISK:
        err = DVDERR_NO_DISK;
        break;

      case DVD_STATE_COVER_OPEN:
        err = DVDERR_COVER_OPEN;
        break;

      case DVD_STATE_WRONG_DISK:
        err = DVDERR_WRONG_DISK;
        break;

      case DVD_STATE_RETRY:
        err = DVDERR_RETRY;
        break;
    }
  }

  return err;
}

static void dvderr_draw_CoverOpen() {
  gSPDisplayList(this->gfx_p++, keikoku1_dvd_att_winT_model);
  gSPDisplayList(this->gfx_p++, keikoku1_dvd_att_moji_model);
}

static void dvderr_draw_NoDisk() {
  gSPDisplayList(this->gfx_p++, keikoku2_dvd_att_winT_model);
  gSPDisplayList(this->gfx_p++, keikoku2_dvd_att_moji_model);
}

static void dvderr_draw_WrongDisk() {
  gSPDisplayList(this->gfx_p++, keikoku3_dvd_att_winT_model);
  gSPDisplayList(this->gfx_p++, keikoku3_dvd_att_moji_model);
}

static void dvderr_draw_Retry() {
  gSPDisplayList(this->gfx_p++, keikoku4_dvd_att_winT_model);
  gSPDisplayList(this->gfx_p++, keikoku4_dvd_att_moji_model);
}

static void dvderr_draw_Fatal() {
  gSPDisplayList(this->gfx_p++, keikoku5_dvd_att_winT_model);
  gSPDisplayList(this->gfx_p++, keikoku5_dvd_att_moji_model);
}

static void dvderr_mtx_set() {
  guScale(&this->modelview, this->scale, this->scale, 1.0f);
}

#define DVDERR_MOVE_SPEED (1.0f/18.0f)

static void dvderr_move_up();
static void dvderr_move_down();
static void dvderr_move_wait();

static void dvderr_move_up() {
  this->scale += DVDERR_MOVE_SPEED;
  if (this->scale >= 1.0f) {
    this->scale = 1.0f;
  }

  if (this->next_error != this->now_error) {
    this->draw_proc = &dvderr_move_down;
  }
}

static void dvderr_move_down() {
  this->scale -= DVDERR_MOVE_SPEED;
  if (this->scale <= 0.0f) {
    this->scale = 0.0f;
    this->now_error = this->next_error;
    this->draw_proc = &dvderr_move_wait;
  }
}

static void dvderr_move_wait() {
  if (this->next_error == DVDERR_NONE) {
    return;
  }

  this->now_error = this->next_error;
  this->draw_proc = &dvderr_move_up;
}

static void dvderr_move() {
  (*this->draw_proc)();
}

extern int dvderr_draw() {
  static const DVDERR_DRAW_PROC draw_proc[DVDERR_NUM] = {
    &dvderr_draw_CoverOpen,
    &dvderr_draw_NoDisk,
    &dvderr_draw_WrongDisk,
    &dvderr_draw_Retry,
    &dvderr_draw_Fatal
  };

  this->next_error = dvderr_check_drive();
  dvderr_move();

  if (this->now_error >= DVDERR_COVER_OPEN) {
    dvderr_mtx_set();
    JW_BeginFrame();
    this->gfx_p = this->gfx;
    gSPDisplayList(this->gfx_p++, Dvderr_initial_dl);

    if (draw_proc[this->now_error] != NULL) {
      (*draw_proc[this->now_error])();
    }

    gDPFullSync(this->gfx_p++);
    gSPEndDisplayList(this->gfx_p++);

    dvderr_exec_dl(this->gfx);
    JW_EndFrame();
    VISetBlack(FALSE);
    return TRUE;
  }
  else {
    return FALSE;
  }
}

extern void dvderr_init() {
  bzero(this, sizeof(dvderr_work));
  this->draw_proc = &dvderr_move_wait;
  this->now_error = DVDERR_NONE;
  this->next_error = DVDERR_NONE;

  guOrtho(&this->ortho, -160.0f, 160.0f, -120.0f, 120.0f, -800.0f, 800.0f, 1.0f); /* TODO: N64 screen size macros (float) */
  guLookAt(&this->projection, 0.0f, 0.0f, 400.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f);
}

