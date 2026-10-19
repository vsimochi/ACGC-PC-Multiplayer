/* pc_version.h - THE single source of the game's version number.
 *
 * Bumping the version for a release = edit the four numbers below and nothing else in the code: the Windows version resource (pc/res/app.rc includes this
 * header), the title screen corner text, the startup log line and the dedicated server banner / `status` all derive from them.
 *   - while developing X.Y.Z:   PC_VERSION_DEV 1  -> the game shows "X.Y.Z-dev"
 *   - the release commit:       PC_VERSION_DEV 0  -> the game shows "X.Y.Z"
 *   - right after the release:  bump PATCH (or MINOR) and set PC_VERSION_DEV back to 1
 * The exe's version resource is always the numeric "X.Y.Z.0" (FILEVERSION / PRODUCTVERSION and both version strings; no "-dev" suffix there).
 * The release scripts (release_<ver>/scripts/rebuild_from_commit.sh) read these macros from the release commit and refuse a commit whose numbers or DEV flag
 * do not match the release. Plain #defines only: the header is also read by the resource compiler (windres) and by the release scripts (grep). */
#ifndef PC_VERSION_H
#define PC_VERSION_H

#define PC_VERSION_MAJOR 0
#define PC_VERSION_MINOR 4
#define PC_VERSION_PATCH 1
#define PC_VERSION_DEV   0

#define PC_VERSION_STR_(x) #x
#define PC_VERSION_XSTR(x) PC_VERSION_STR_(x)

/* "0.3.1" */
#define PC_VERSION_BASE_STRING PC_VERSION_XSTR(PC_VERSION_MAJOR.PC_VERSION_MINOR.PC_VERSION_PATCH)
/* "0.3.1.0": the version resource strings */
#define PC_VERSION_NUMERIC_STRING PC_VERSION_XSTR(PC_VERSION_MAJOR.PC_VERSION_MINOR.PC_VERSION_PATCH.0)
/* 0,3,1,0: FILEVERSION / PRODUCTVERSION of the version resource */
#define PC_VERSION_RC_NUMBERS PC_VERSION_MAJOR,PC_VERSION_MINOR,PC_VERSION_PATCH,0

/* "0.3.1-dev" while developing, "0.3.1" in the release (what the title screen, the log and the dedicated console show, with a leading 'v') */
#if PC_VERSION_DEV
#define PC_VERSION_STRING PC_VERSION_BASE_STRING "-dev"
#else
#define PC_VERSION_STRING PC_VERSION_BASE_STRING
#endif

#endif /* PC_VERSION_H */
