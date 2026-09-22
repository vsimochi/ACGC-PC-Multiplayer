#ifndef PC_SPLIT_H
#define PC_SPLIT_H

/* Data split out of mixed code+data files for the experimental PC_LOW_ADDRESS_64 build.
 * The *_gfx.c_inc / *_data.c_inc files are #included in place everywhere except in the
 * src/data/pc_split/*.c translation units (compiled as C++, which allows the static
 * (unsigned int)(uintptr_t)&sym initializers). There the symbols the original code still
 * uses must have external linkage; PC_SPLIT_STATIC is `static` in every other build. */
#ifdef PC_SPLIT_TU
#define PC_SPLIT_STATIC
#else
#define PC_SPLIT_STATIC static
#endif

#endif /* PC_SPLIT_H */
