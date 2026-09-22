#include "m_fbdemo_wipe1.h"

#include "gfxalloc.h"
#include "libultra/gu.h"
#include "libultra/libultra.h"
#include "m_common_data.h"
#include "m_rcp.h"

#ifdef PC_LOW_ADDRESS_64
#include "../src/data/pc_split/m_fbdemo_wipe1_split.h"
#else
#include "../src/game/m_fbdemo_wipe1_gfx.c_inc"
#endif

fbdemo_wipe1* fbdemo_wipe1_init(fbdemo_wipe1* this) {
  bzero(this, sizeof(fbdemo_wipe1));
  return this;
}

void fbdemo_wipe1_move(fbdemo_wipe1* this, int rate) {
  static f32 wipe1_accum = 0.0f;
  f32 dt = (f32)gamePT->graph->dt_num_60fps_frames;
  f32 inc = ((f32)Common_Get(transition).wipe_rate * 3.0f * dt) / (f32)rate;

  if (this->direction != 0) {
    wipe1_accum += inc;
    int steps = (int)wipe1_accum;
    wipe1_accum -= (f32)steps;
    this->texY += steps;
    if (this->texY >= (int)(153 * (1 << 2))) {
      this->texY = (int)(153 * (1 << 2));
      this->finished = 1;
    }
  } else {
    wipe1_accum += inc;
    int steps = (int)wipe1_accum;
    wipe1_accum -= (f32)steps;
    this->texY -= steps;
    if (this->texY <= (int)(83.25f * (1 << 2))) {
      this->texY = (int)(83.25f * (1 << 2));
      this->finished = 1;
    }
  }
}

void fbdemo_wipe1_draw(fbdemo_wipe1* this, Gfx** gfxP) {
  Gfx* gfx = *gfxP;
  Mtx* modelView;
  int pad[4];
  Gfx* texScroll;

  modelView = this->modelView[this->frame];
  this->frame ^= 1;

  guScale(&modelView[0], 0.56f, 0.56f, 1.0f);
  guRotate(&modelView[1], 0.0f, 0.0f, 0.0f, 1.0f);
  guTranslate(&modelView[2], 0.0f, 0.0f, 0.0f);
  gDPPipeSync(gfx++);

  texScroll = gfx_tex_scroll2(&gfx, this->texX, this->texY, 0, 0);
  gSPSegment(gfx++, 8, texScroll);

  gDPSetPrimColor(gfx++, 0, 0x80, this->color.r, this->color.g, this->color.b,
                  255);

#ifdef TARGET_PC
  /* Copy matrices into gfxalloc buffers so their addresses come from the DL
     buffer instead of the heap (which can collide with N64 segment range
     0x03-0x0F, causing seg2k0 misidentification). */
  {
    Mtx* proj_copy = (Mtx*)gfxalloc(&gfx, sizeof(Mtx));
    Mtx* look_copy = (Mtx*)gfxalloc(&gfx, sizeof(Mtx));
    Mtx* mv0_copy = (Mtx*)gfxalloc(&gfx, sizeof(Mtx));
    Mtx* mv1_copy = (Mtx*)gfxalloc(&gfx, sizeof(Mtx));
    Mtx* mv2_copy = (Mtx*)gfxalloc(&gfx, sizeof(Mtx));
    *proj_copy = this->projection;
    *look_copy = this->lookAt;
    *mv0_copy = modelView[0];
    *mv1_copy = modelView[1];
    *mv2_copy = modelView[2];
    gSPMatrix(gfx++, proj_copy, G_MTX_LOAD | G_MTX_PROJECTION);
    gSPPerspNormalize(gfx++, this->normal);
    gSPMatrix(gfx++, look_copy, G_MTX_MUL | G_MTX_PROJECTION);
    gSPMatrix(gfx++, mv0_copy, G_MTX_NOPUSH | G_MTX_LOAD | G_MTX_MODELVIEW);
    gSPMatrix(gfx++, mv1_copy, G_MTX_NOPUSH | G_MTX_MUL | G_MTX_MODELVIEW);
    gSPMatrix(gfx++, mv2_copy, G_MTX_NOPUSH | G_MTX_MUL | G_MTX_MODELVIEW);
  }
#else
  gSPMatrix(gfx++, &this->projection, G_MTX_LOAD | G_MTX_PROJECTION);
  gSPPerspNormalize(gfx++, this->normal);

  gSPMatrix(gfx++, &this->lookAt, G_MTX_MUL | G_MTX_PROJECTION);
  gSPMatrix(gfx++, &modelView[0], G_MTX_NOPUSH | G_MTX_LOAD | G_MTX_MODELVIEW);
  gSPMatrix(gfx++, &modelView[1], G_MTX_NOPUSH | G_MTX_MUL | G_MTX_MODELVIEW);
  gSPMatrix(gfx++, &modelView[2], G_MTX_NOPUSH | G_MTX_MUL | G_MTX_MODELVIEW);
#endif

  gSPDisplayList(gfx++, wipe1_modelT);
  gDPPipeSync(gfx++);
  *gfxP = gfx;
}

void fbdemo_wipe1_startup(fbdemo_wipe1* this) {
  this->finished = 0;

  if (this->direction != 0) {
    this->texY = (int)(83.25f * (1 << 2));
  } else {
    this->texY = (int)(153.0f * (1 << 2));
  }

  guPerspective(&this->projection, &this->normal, 60.0f, 1.33333337307f, 10.0f,
                12800.0f, 1.0f);
  guLookAt(&this->lookAt, 0.0f, 0.0f, 400.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f,
           0.0f);
}

void fbdemo_wipe1_settype(fbdemo_wipe1* this, int type) {
  if (type == 1) {
    this->direction = 1;
    this->texY = 0x14D;
    return;
  }
  this->direction = 0;
  this->texY = 0x264;
}

void fbdemo_wipe1_setcolor_rgba8888(fbdemo_wipe1* this, u32 color) {
  this->color.rgba = color;
}

u8 fbdemo_wipe1_is_finish(fbdemo_wipe1* this) { return this->finished; }
