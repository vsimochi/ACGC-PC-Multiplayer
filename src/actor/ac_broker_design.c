#include "ac_broker_design.h"

#include "m_common_data.h"

static void Broker_Design_Actor_ct(ACTOR* actorx, GAME* game);
static void Broker_Design_Actor_dt(ACTOR* actorx, GAME* game);
static void Broker_Design_Actor_move(ACTOR* actorx, GAME* game);
static void Broker_Design_Actor_draw(ACTOR* actorx, GAME* game);

static mActor_name_t aBD_UnitNo2FtrNo(int ux, int uz);
static int aBD_ReportFtrSales(int ux, int uz);

// clang-format off
ACTOR_PROFILE Broker_Design_Profile = {
    mAc_PROFILE_BROKER_DESIGN,
    ACTOR_PART_BG,
    ACTOR_STATE_NO_DRAW_WHILE_CULLED | ACTOR_STATE_NO_MOVE_WHILE_CULLED,
    EMPTY_NO,
    ACTOR_OBJ_BANK_KEEP,
    sizeof(BROKER_DESIGN_ACTOR),
    &Broker_Design_Actor_ct,
    &Broker_Design_Actor_dt,
    &Broker_Design_Actor_move,
    &Broker_Design_Actor_draw,
    NULL,
};
// clang-format on

typedef struct broker_set_data_s {
    int ux;
    int uz;
} aBD_set_data_c;

static void aBD_SetFurnitureFg(ACTOR* actorx) {
    mActor_name_t* item_p = Save_Get(event_save_data).special.event.broker.items;
    int i;
    mActor_name_t* fg_p = mFI_BkNumtoUtFGTop(0, 0);
    static aBD_set_data_c set_data[] = { { 2, 2 }, { 4, 2 }, { 2, 4 } };

    for (i = 0; i < 3; i++) {
        // nothing
    }

    for (i = 0; i < 3; i++) {
        int ut;

        ut = set_data[i].ux + set_data[i].uz * UT_X_NUM;
        if (item_p[i] == EMPTY_NO) {
            fg_p[ut] = RSV_SHOP_SOLD_FTR;
        } else if (ITEM_IS_FTR(item_p[i])) {
            fg_p[ut] = item_p[i];
        }
    }
}

static void Broker_Design_Actor_ct(ACTOR* actorx, GAME* game) {
    BROKER_DESIGN_ACTOR* broker_design = (BROKER_DESIGN_ACTOR*)actorx;

    if (mEv_CheckEvent(mEv_SPNPC_BROKER)) {
        aBD_SetFurnitureFg(actorx);
    }

    broker_design->clip.get_no_proc = &aBD_UnitNo2FtrNo;
    broker_design->clip.report_sales_proc = &aBD_ReportFtrSales;
    CLIP(broker_design_clip) = &broker_design->clip;
}

static void Broker_Design_Actor_dt(ACTOR* actorx, GAME* game) {
    CLIP(broker_design_clip) = NULL;
}

static void Broker_Design_Actor_draw(ACTOR* actorx, GAME* game) {
    // nothing
}

static void Broker_Design_Actor_move(ACTOR* actorx, GAME* game) {
    // nothing
}

static mActor_name_t aBD_UnitNo2FtrNo(int ux, int uz) {
    int bx;
    int bz;
    int block_ux;
    int block_uz;
    mActor_name_t ftr;

    bx = ux / UT_X_NUM;
    bz = uz / UT_Z_NUM;
    block_ux = ux - bx * UT_X_NUM;
    block_uz = uz - bz * UT_Z_NUM;
    if (aMR_CLIP != NULL) {
        if (aMR_CLIP->unitNum2ftrItemNoftrId_proc(&ftr, NULL, block_ux, block_uz, mCoBG_LAYER0) && ITEM_IS_FTR(ftr)) {
            return ftr;
        }
    }

    return RSV_NO;
}

#ifdef TARGET_PC
/* Shared event NPCs (worker E, pc_evnpc_shared.h): a piece another player bought (the host cleared it in the lineup; service 5 brought it here) leaves this tent's display
 * the way aBD_ReportFtrSales removes a sold one; the lineup itself is not touched. Position i shows items[i] (aBD_SetFurnitureFg). Returns how many pieces were removed. */
int aBD_PC_SyncDisplayWithLineup(void) {
    static const int pos[3][2] = { { 2, 2 }, { 4, 2 }, { 2, 4 } };
    mActor_name_t* items = Save_Get(event_save_data).special.event.broker.items;
    mActor_name_t* fg_p;
    int i, n = 0;

    if (CLIP(broker_design_clip) == NULL || aMR_CLIP == NULL || !mEv_CheckEvent(mEv_SPNPC_BROKER)) {
        return 0;
    }
    fg_p = mFI_BkNumtoUtFGTop(0, 0);
    for (i = 0; i < 3; i++) {
        mActor_name_t ftr = aBD_UnitNo2FtrNo(pos[i][0], pos[i][1]);
        mActor_name_t sold_ftr;
        int sold_ftrID;

        if (items[i] == EMPTY_NO && ITEM_IS_FTR(ftr) &&
            aMR_CLIP->unitNum2ftrItemNoftrId_proc(&sold_ftr, &sold_ftrID, pos[i][0], pos[i][1], mCoBG_LAYER0)) {
            aMR_CLIP->ftrId2extinguishFtr_proc(sold_ftrID);
            if (fg_p != NULL) {
                fg_p[pos[i][0] + pos[i][1] * UT_X_NUM] = RSV_SHOP_SOLD_FTR;
            }
            n++;
        }
    }
    return n;
}
#endif

static int aBD_ReportFtrSales(int ux, int uz) {
    mActor_name_t ftr = aBD_UnitNo2FtrNo(ux, uz);

    if (ITEM_IS_FTR(ftr) && aMR_CLIP != NULL) {
        mActor_name_t sold_ftr;
        int sold_ftrID;
        mActor_name_t* fg_p;
        int local_ux;
        int local_uz;
        int local_ut;

        fg_p = mFI_BkNumtoUtFGTop(0, 0);
        local_ux = ux & 0xF;
        local_uz = uz & 0xF;
        local_ut = local_ux + local_uz * UT_X_NUM;

        if (aMR_CLIP->unitNum2ftrItemNoftrId_proc(&sold_ftr, &sold_ftrID, ux, uz, mCoBG_LAYER0)) {
            int i;
            mActor_name_t* ftr_p = Save_Get(event_save_data).special.event.broker.items;

            aMR_CLIP->ftrId2extinguishFtr_proc(sold_ftrID);
            fg_p[local_ut] = RSV_SHOP_SOLD_FTR;

            for (i = 0; i < 3; i++) {
                if (ftr == ftr_p[i]) {
                    ftr_p[i] = EMPTY_NO;
                    return TRUE;
                }
            }
        }
    }

    return FALSE;
}
