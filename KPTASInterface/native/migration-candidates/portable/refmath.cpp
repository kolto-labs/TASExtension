/*
 * refmath.cpp - deterministic reference math for the portable profile (D12).
 * See refmath.hpp for the contract. pow lives in refmath_pow.cpp.
 *
 * The algorithms, polynomial coefficients and argument reduction below are
 * those of Sun's fdlibm as carried by FreeBSD msun and musl. The text was
 * taken from the musl sources bundled with zig 0.16.0
 * (lib/libc/musl/src/math: __sin.c, __cos.c, __tan.c, __rem_pio2.c,
 * __rem_pio2_large.c, atan2.c, scalbn.c) and from zig's ports of musl
 * (lib/std/math/{asin,acos,atan}.zig, lib/compiler_rt/{sin,cos,tan,exp,
 * log10,log2}.zig). log follows musl's fdlibm-derived log.c, sharing the
 * reduction and coefficients of log10 and log2. Changes: names, double_t
 * replaced by double, floating-point exception side effects (FORCE_EVAL)
 * dropped, and representation access uses C++20 bit_cast. No arithmetic
 * operation or evaluation order is changed.
 *
 * ====================================================
 * Copyright (C) 1993 by Sun Microsystems, Inc. All rights reserved.
 *
 * Developed at SunSoft, a Sun Microsystems, Inc. business.
 * Permission to use, copy, modify, and distribute this
 * software is freely granted, provided that this notice
 * is preserved.
 * ====================================================
 * Copyright (C) 2004 by Sun Microsystems, Inc. All rights reserved.
 * (__tan: permission as above.)
 * ====================================================
 * musl: Copyright (c) 2005-2020 Rich Felker, et al. MIT license:
 * Permission is hereby granted, free of charge, to any person obtaining a
 * copy of this software and associated documentation files (the "Software"),
 * to deal in the Software without restriction, including without limitation
 * the rights to use, copy, modify, merge, publish, distribute, sublicense,
 * and/or sell copies of the Software, and to permit persons to whom the
 * Software is furnished to do so, subject to the following conditions:
 * The above copyright notice and this permission notice shall be included
 * in all copies or substantial portions of the Software.
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
 * ====================================================
 *
 * Build: -O2 -msse2 -mfpmath=sse -ffp-contract=off -ffreestanding
 *        -fno-builtin -fno-fast-math. -ffp-contract=off is required: a fused
 *        multiply-add would change results on CPUs that have FMA.
 */
#include <bit>
#include <limits>
#include <stdint.h>
#include "refmath.hpp"

static_assert(sizeof(double) == sizeof(uint64_t) &&
              std::numeric_limits<double>::is_iec559 &&
              std::numeric_limits<double>::digits == 53 &&
              std::numeric_limits<double>::max_exponent == 1024,
              "refmath requires IEEE-754 binary64 doubles");

#if defined(__i386__) && !defined(__SSE2_MATH__)
#error "refmath must be built with -msse2 -mfpmath=sse on i386"
#endif
#if defined(__FAST_MATH__)
#error "refmath must not be built with -ffast-math"
#endif

static inline uint64_t asu64(double x) { return std::bit_cast<uint64_t>(x); }
static inline double asdbl(uint64_t i) { return std::bit_cast<double>(i); }
static inline uint32_t hiword(double x) { return (uint32_t)(asu64(x) >> 32); }
static inline uint32_t loword(double x) { return (uint32_t)asu64(x); }
static inline double lo0(double x) { return asdbl(asu64(x) & 0xffffffff00000000ULL); }
static inline double rm_fabs(double x) { return asdbl(asu64(x) & 0x7fffffffffffffffULL); }
static inline int rm_isnan(double x) { return (asu64(x) << 1) > 0xffe0000000000000ULL; }

/* ------------------------------------------------------------ sqrt, floor */

double refmath_sqrt(double x)
{
	/* sqrtsd is correctly rounded by IEEE-754 on every SSE2 CPU. */
	__asm__("sqrtsd %1, %0" : "=x"(x) : "x"(x));
	return x;
}

/* floor and ceil are exact; integer implementation, independent of the
 * rounding mode. */
double refmath_floor(double x)
{
	uint64_t u = asu64(x), m;
	int e = (int)((u >> 52) & 0x7ff) - 0x3ff;
	if (e >= 52)
		return x;                       /* integral, inf or nan */
	if (e < 0) {                            /* |x| < 1 */
		if ((u << 1) == 0)
			return x;               /* +-0 */
		return (u >> 63) ? -1.0 : 0.0;
	}
	m = 0x000fffffffffffffULL >> e;
	if ((u & m) == 0)
		return x;
	if (u >> 63)
		u += m;
	u &= ~m;
	return asdbl(u);
}

double refmath_ceil(double x)
{
	uint64_t u = asu64(x), m;
	int e = (int)((u >> 52) & 0x7ff) - 0x3ff;
	if (e >= 52)
		return x;
	if (e < 0) {
		if ((u << 1) == 0)
			return x;
		return (u >> 63) ? -0.0 : 1.0;
	}
	m = 0x000fffffffffffffULL >> e;
	if ((u & m) == 0)
		return x;
	if (!(u >> 63))
		u += m;
	u &= ~m;
	return asdbl(u);
}

/* ------------------------------------------------------------ scalbn (musl) */

double refmath_scalbn(double x, int n)
{
	double y = x;
	if (n > 1023) {
		y *= 0x1p1023;
		n -= 1023;
		if (n > 1023) {
			y *= 0x1p1023;
			n -= 1023;
			if (n > 1023)
				n = 1023;
		}
	} else if (n < -1022) {
		/* make sure final n < -53 to avoid double
		   rounding in the subnormal range */
		y *= 0x1p-1022 * 0x1p53;
		n += 1022 - 53;
		if (n < -1022) {
			y *= 0x1p-1022 * 0x1p53;
			n += 1022 - 53;
			if (n < -1022)
				n = -1022;
		}
	}
	return y * asdbl((uint64_t)(0x3ff + n) << 52);
}

/* ------------------------------------------------------------ fmod (musl) */

double refmath_fmod(double x, double y)
{
	uint64_t uxi = asu64(x), uyi = asu64(y), i;
	int ex = (int)(uxi >> 52 & 0x7ff);
	int ey = (int)(uyi >> 52 & 0x7ff);
	int sx = (int)(uxi >> 63);

	if (uyi << 1 == 0 || rm_isnan(y) || ex == 0x7ff)
		return (x * y) / (x * y);
	if (uxi << 1 <= uyi << 1) {
		if (uxi << 1 == uyi << 1)
			return 0 * x;
		return x;
	}
	/* normalize x and y */
	if (!ex) {
		for (i = uxi << 12; i >> 63 == 0; ex--, i <<= 1)
			;
		uxi <<= -ex + 1;
	} else {
		uxi &= -1ULL >> 12;
		uxi |= 1ULL << 52;
	}
	if (!ey) {
		for (i = uyi << 12; i >> 63 == 0; ey--, i <<= 1)
			;
		uyi <<= -ey + 1;
	} else {
		uyi &= -1ULL >> 12;
		uyi |= 1ULL << 52;
	}
	/* x mod y */
	for (; ex > ey; ex--) {
		i = uxi - uyi;
		if (i >> 63 == 0) {
			if (i == 0)
				return 0 * x;
			uxi = i;
		}
		uxi <<= 1;
	}
	i = uxi - uyi;
	if (i >> 63 == 0) {
		if (i == 0)
			return 0 * x;
		uxi = i;
	}
	for (; uxi >> 52 == 0; uxi <<= 1, ex--)
		;
	/* scale result */
	if (ex > 0) {
		uxi -= 1ULL << 52;
		uxi |= (uint64_t)ex << 52;
	} else {
		uxi >>= -ex + 1;
	}
	uxi |= (uint64_t)sx << 63;
	return asdbl(uxi);
}

/* ------------------------------------------------------------ trig kernels */

static const double
S1  = -1.66666666666666324348e-01, /* 0xBFC55555, 0x55555549 */
S2  =  8.33333333332248946124e-03, /* 0x3F811111, 0x1110F8A6 */
S3  = -1.98412698298579493134e-04, /* 0xBF2A01A0, 0x19C161D5 */
S4  =  2.75573137070700676789e-06, /* 0x3EC71DE3, 0x57B1FE7D */
S5  = -2.50507602534068634195e-08, /* 0xBE5AE5E6, 0x8A2B9CEB */
S6  =  1.58969099521155010221e-10; /* 0x3DE5D93A, 0x5ACFD57C */

static double k_sin(double x, double y, int iy)
{
	double z, r, v, w;
	z = x*x;
	w = z*z;
	r = S2 + z*(S3 + z*S4) + z*w*(S5 + z*S6);
	v = z*x;
	if (iy == 0)
		return x + v*(S1 + z*r);
	else
		return x - ((z*(0.5*y - v*r) - y) - v*S1);
}

static const double
C1  =  4.16666666666666019037e-02, /* 0x3FA55555, 0x5555554C */
C2  = -1.38888888888741095749e-03, /* 0xBF56C16C, 0x16C15177 */
C3  =  2.48015872894767294178e-05, /* 0x3EFA01A0, 0x19CB1590 */
C4  = -2.75573143513906633035e-07, /* 0xBE927E4F, 0x809C52AD */
C5  =  2.08757232129817482790e-09, /* 0x3E21EE9E, 0xBDB4B1C4 */
C6  = -1.13596475577881948265e-11; /* 0xBDA8FAE9, 0xBE8838D4 */

static double k_cos(double x, double y)
{
	double hz, z, r, w;
	z  = x*x;
	w  = z*z;
	r  = z*(C1+z*(C2+z*C3)) + w*w*(C4+z*(C5+z*C6));
	hz = 0.5*z;
	w  = 1.0-hz;
	return w + (((1.0-w)-hz) + (z*r-x*y));
}

static const double T[] = {
	 3.33333333333334091986e-01, /* 3FD55555, 55555563 */
	 1.33333333333201242699e-01, /* 3FC11111, 1110FE7A */
	 5.39682539762260521377e-02, /* 3FABA1BA, 1BB341FE */
	 2.18694882948595424599e-02, /* 3F9664F4, 8406D637 */
	 8.86323982359930005737e-03, /* 3F8226E3, E96E8493 */
	 3.59207910759131235356e-03, /* 3F6D6D22, C9560328 */
	 1.45620945432529025516e-03, /* 3F57DBC8, FEE08315 */
	 5.88041240820264096874e-04, /* 3F4344D8, F2F26501 */
	 2.46463134818469906812e-04, /* 3F3026F7, 1A8D1068 */
	 7.81794442939557092300e-05, /* 3F147E88, A03792A6 */
	 7.14072491382608190305e-05, /* 3F12B80F, 32F0A7E9 */
	-1.85586374855275456654e-05, /* BEF375CB, DB605373 */
	 2.59073051863633712884e-05, /* 3EFB2A70, 74BF7AD4 */
},
pio4 =       7.85398163397448278999e-01, /* 3FE921FB, 54442D18 */
pio4lo =     3.06161699786838301793e-17; /* 3C81A626, 33145C07 */

static double k_tan(double x, double y, int odd)
{
	double z, r, v, w, s, a;
	double w0, a0;
	uint32_t hx;
	int big, sign = 0;

	hx = hiword(x);
	big = (hx&0x7fffffff) >= 0x3FE59428; /* |x| >= 0.6744 */
	if (big) {
		sign = hx>>31;
		if (sign) {
			x = -x;
			y = -y;
		}
		x = (pio4 - x) + (pio4lo - y);
		y = 0.0;
	}
	z = x * x;
	w = z * z;
	r = T[1] + w*(T[3] + w*(T[5] + w*(T[7] + w*(T[9] + w*T[11]))));
	v = z*(T[2] + w*(T[4] + w*(T[6] + w*(T[8] + w*(T[10] + w*T[12])))));
	s = z * x;
	r = y + z*(s*(r + v) + y) + s*T[0];
	w = x + r;
	if (big) {
		s = 1 - 2*odd;
		v = s - 2.0 * (x + (r - w*w/(w + s)));
		return sign ? -v : v;
	}
	if (!odd)
		return w;
	/* -1.0/(x+r) has up to 2ulp error, so compute it accurately */
	w0 = lo0(w);
	v = r - (w0 - x);       /* w0+v = r+x */
	a0 = a = -1.0 / w;
	a0 = lo0(a0);
	return a0 + a*(1.0 + a0*w0 + a0*v);
}

/* ------------------------------------------------------------ rem_pio2 */

static const int init_jk[] = {3,4,4,6}; /* initial value for jk */

/* 2/pi in 24-bit chunks; enough terms for double (e0 <= 1023). */
static const int32_t ipio2[] = {
0xA2F983, 0x6E4E44, 0x1529FC, 0x2757D1, 0xF534DD, 0xC0DB62,
0x95993C, 0x439041, 0xFE5163, 0xABDEBB, 0xC561B7, 0x246E3A,
0x424DD2, 0xE00649, 0x2EEA09, 0xD1921C, 0xFE1DEB, 0x1CB129,
0xA73EE8, 0x8235F5, 0x2EBB44, 0x84E99C, 0x7026B4, 0x5F7E41,
0x3991D6, 0x398353, 0x39F49C, 0x845F8B, 0xBDF928, 0x3B1FF8,
0x97FFDE, 0x05980F, 0xEF2F11, 0x8B5A0A, 0x6D1F6D, 0x367ECF,
0x27CB09, 0xB74F46, 0x3F669E, 0x5FEA2D, 0x7527BA, 0xC7EBE5,
0xF17B3D, 0x0739F7, 0x8A5292, 0xEA6BFB, 0x5FB11F, 0x8D5D08,
0x560330, 0x46FC7B, 0x6BABF0, 0xCFBC20, 0x9AF436, 0x1DA9E3,
0x91615E, 0xE61B08, 0x659985, 0x5F14A0, 0x68408D, 0xFFD880,
0x4D7327, 0x310606, 0x1556CA, 0x73A8C9, 0x60E27B, 0xC08C6B,
};

static const double PIo2[] = {
  1.57079625129699707031e+00, /* 0x3FF921FB, 0x40000000 */
  7.54978941586159635335e-08, /* 0x3E74442D, 0x00000000 */
  5.39030252995776476554e-15, /* 0x3CF84698, 0x80000000 */
  3.28200341580791294123e-22, /* 0x3B78CC51, 0x60000000 */
  1.27065575308067607349e-29, /* 0x39F01B83, 0x80000000 */
  1.22933308981111328932e-36, /* 0x387A2520, 0x40000000 */
  2.73370053816464559624e-44, /* 0x36E38222, 0x80000000 */
  2.16741683877804819444e-51, /* 0x3569F31D, 0x00000000 */
};

static int rem_pio2_large(double *x, double *y, int e0, int nx, int prec)
{
	int32_t jz,jx,jv,jp,jk,carry,n,iq[20],i,j,k,m,q0,ih;
	double z,fw,f[20],fq[20],q[20];

	/* initialize jk*/
	jk = init_jk[prec];
	jp = jk;

	/* determine jx,jv,q0, note that 3>q0 */
	jx = nx-1;
	jv = (e0-3)/24;  if(jv<0) jv=0;
	q0 = e0-24*(jv+1);

	/* set up f[0] to f[jx+jk] where f[jx+jk] = ipio2[jv+jk] */
	j = jv-jx; m = jx+jk;
	for (i=0; i<=m; i++,j++)
		f[i] = j<0 ? 0.0 : (double)ipio2[j];

	/* compute q[0],q[1],...q[jk] */
	for (i=0; i<=jk; i++) {
		for (j=0,fw=0.0; j<=jx; j++)
			fw += x[j]*f[jx+i-j];
		q[i] = fw;
	}

	jz = jk;
recompute:
	/* distill q[] into iq[] reversingly */
	for (i=0,j=jz,z=q[jz]; j>0; i++,j--) {
		fw    = (double)(int32_t)(0x1p-24*z);
		iq[i] = (int32_t)(z - 0x1p24*fw);
		z     = q[j-1]+fw;
	}

	/* compute n */
	z  = refmath_scalbn(z,q0);       /* actual value of z */
	z -= 8.0*refmath_floor(z*0.125); /* trim off integer >= 8 */
	n  = (int32_t)z;
	z -= (double)n;
	ih = 0;
	if (q0 > 0) {  /* need iq[jz-1] to determine n */
		i  = iq[jz-1]>>(24-q0); n += i;
		iq[jz-1] -= i<<(24-q0);
		ih = iq[jz-1]>>(23-q0);
	}
	else if (q0 == 0) ih = iq[jz-1]>>23;
	else if (z >= 0.5) ih = 2;

	if (ih > 0) {  /* q > 0.5 */
		n += 1; carry = 0;
		for (i=0; i<jz; i++) {  /* compute 1-q */
			j = iq[i];
			if (carry == 0) {
				if (j != 0) {
					carry = 1;
					iq[i] = 0x1000000 - j;
				}
			} else
				iq[i] = 0xffffff - j;
		}
		if (q0 > 0) {  /* rare case: chance is 1 in 12 */
			switch(q0) {
			case 1:
				iq[jz-1] &= 0x7fffff; break;
			case 2:
				iq[jz-1] &= 0x3fffff; break;
			}
		}
		if (ih == 2) {
			z = 1.0 - z;
			if (carry != 0)
				z -= refmath_scalbn(1.0,q0);
		}
	}

	/* check if recomputation is needed */
	if (z == 0.0) {
		j = 0;
		for (i=jz-1; i>=jk; i--) j |= iq[i];
		if (j == 0) {  /* need recomputation */
			for (k=1; iq[jk-k]==0; k++);  /* k = no. of terms needed */

			for (i=jz+1; i<=jz+k; i++) {  /* add q[jz+1] to q[jz+k] */
				f[jx+i] = (double)ipio2[jv+i];
				for (j=0,fw=0.0; j<=jx; j++)
					fw += x[j]*f[jx+i-j];
				q[i] = fw;
			}
			jz += k;
			goto recompute;
		}
	}

	/* chop off zero terms */
	if (z == 0.0) {
		jz -= 1;
		q0 -= 24;
		while (iq[jz] == 0) {
			jz--;
			q0 -= 24;
		}
	} else { /* break z into 24-bit if necessary */
		z = refmath_scalbn(z,-q0);
		if (z >= 0x1p24) {
			fw = (double)(int32_t)(0x1p-24*z);
			iq[jz] = (int32_t)(z - 0x1p24*fw);
			jz += 1;
			q0 += 24;
			iq[jz] = (int32_t)fw;
		} else
			iq[jz] = (int32_t)z;
	}

	/* convert integer "bit" chunk to floating-point value */
	fw = refmath_scalbn(1.0,q0);
	for (i=jz; i>=0; i--) {
		q[i] = fw*(double)iq[i];
		fw *= 0x1p-24;
	}

	/* compute PIo2[0,...,jp]*q[jz,...,0] */
	for(i=jz; i>=0; i--) {
		for (fw=0.0,k=0; k<=jp && k<=jz-i; k++)
			fw += PIo2[k]*q[i+k];
		fq[jz-i] = fw;
	}

	/* compress fq[] into y[] (prec 1: double) */
	fw = 0.0;
	for (i=jz; i>=0; i--)
		fw += fq[i];
	y[0] = ih==0 ? fw : -fw;
	fw = fq[0]-fw;
	for (i=1; i<=jz; i++)
		fw += fq[i];
	y[1] = ih==0 ? fw : -fw;
	(void)prec;
	return n&7;
}

static const double
toint   = 1.5/0x1p-52, /* 1.5/DBL_EPSILON */
pio4r   = 0x1.921fb54442d18p-1,
invpio2 = 6.36619772367581382433e-01, /* 0x3FE45F30, 0x6DC9C883 */
pio2_1  = 1.57079632673412561417e+00, /* 0x3FF921FB, 0x54400000 */
pio2_1t = 6.07710050650619224932e-11, /* 0x3DD0B461, 0x1A626331 */
pio2_2  = 6.07710050630396597660e-11, /* 0x3DD0B461, 0x1A600000 */
pio2_2t = 2.02226624879595063154e-21, /* 0x3BA3198A, 0x2E037073 */
pio2_3  = 2.02226624871116645580e-21, /* 0x3BA3198A, 0x2E000000 */
pio2_3t = 8.47842766036889956997e-32; /* 0x397B839A, 0x252049C1 */

/* caller must handle the case when reduction is not needed: |x| ~<= pi/4 */
static int rem_pio2(double x, double *y)
{
	uint64_t ui = asu64(x);
	double z,w,t,r,fn;
	double tx[3],ty[2];
	uint32_t ix;
	int sign, n, ex, ey, i;

	sign = (int)(ui>>63);
	ix = (uint32_t)(ui>>32) & 0x7fffffff;
	if (ix <= 0x400f6a7a) {  /* |x| ~<= 5pi/4 */
		if ((ix & 0xfffff) == 0x921fb)  /* |x| ~= pi/2 or 2pi/2 */
			goto medium;  /* cancellation -- use medium case */
		if (ix <= 0x4002d97c) {  /* |x| ~<= 3pi/4 */
			if (!sign) {
				z = x - pio2_1;  /* one round good to 85 bits */
				y[0] = z - pio2_1t;
				y[1] = (z-y[0]) - pio2_1t;
				return 1;
			} else {
				z = x + pio2_1;
				y[0] = z + pio2_1t;
				y[1] = (z-y[0]) + pio2_1t;
				return -1;
			}
		} else {
			if (!sign) {
				z = x - 2*pio2_1;
				y[0] = z - 2*pio2_1t;
				y[1] = (z-y[0]) - 2*pio2_1t;
				return 2;
			} else {
				z = x + 2*pio2_1;
				y[0] = z + 2*pio2_1t;
				y[1] = (z-y[0]) + 2*pio2_1t;
				return -2;
			}
		}
	}
	if (ix <= 0x401c463b) {  /* |x| ~<= 9pi/4 */
		if (ix <= 0x4015fdbc) {  /* |x| ~<= 7pi/4 */
			if (ix == 0x4012d97c)  /* |x| ~= 3pi/2 */
				goto medium;
			if (!sign) {
				z = x - 3*pio2_1;
				y[0] = z - 3*pio2_1t;
				y[1] = (z-y[0]) - 3*pio2_1t;
				return 3;
			} else {
				z = x + 3*pio2_1;
				y[0] = z + 3*pio2_1t;
				y[1] = (z-y[0]) + 3*pio2_1t;
				return -3;
			}
		} else {
			if (ix == 0x401921fb)  /* |x| ~= 4pi/2 */
				goto medium;
			if (!sign) {
				z = x - 4*pio2_1;
				y[0] = z - 4*pio2_1t;
				y[1] = (z-y[0]) - 4*pio2_1t;
				return 4;
			} else {
				z = x + 4*pio2_1;
				y[0] = z + 4*pio2_1t;
				y[1] = (z-y[0]) + 4*pio2_1t;
				return -4;
			}
		}
	}
	if (ix < 0x413921fb) {  /* |x| ~< 2^20*(pi/2), medium size */
medium:
		/* rint(x/(pi/2)) */
		fn = x*invpio2 + toint - toint;
		n = (int32_t)fn;
		r = x - fn*pio2_1;
		w = fn*pio2_1t;  /* 1st round, good to 85 bits */
		/* Matters with directed rounding. */
		if (r - w < -pio4r) {
			n--;
			fn--;
			r = x - fn*pio2_1;
			w = fn*pio2_1t;
		} else if (r - w > pio4r) {
			n++;
			fn++;
			r = x - fn*pio2_1;
			w = fn*pio2_1t;
		}
		y[0] = r - w;
		ey = (int)(asu64(y[0])>>52 & 0x7ff);
		ex = (int)(ix>>20);
		if (ex - ey > 16) { /* 2nd round, good to 118 bits */
			t = r;
			w = fn*pio2_2;
			r = t - w;
			w = fn*pio2_2t - ((t-r)-w);
			y[0] = r - w;
			ey = (int)(asu64(y[0])>>52 & 0x7ff);
			if (ex - ey > 49) {  /* 3rd round, good to 151 bits, covers all cases */
				t = r;
				w = fn*pio2_3;
				r = t - w;
				w = fn*pio2_3t - ((t-r)-w);
				y[0] = r - w;
			}
		}
		y[1] = (r - y[0]) - w;
		return n;
	}
	/*
	 * all other (large) arguments
	 */
	if (ix >= 0x7ff00000) {  /* x is inf or NaN */
		y[0] = y[1] = x - x;
		return 0;
	}
	/* set z = scalbn(|x|,-ilogb(x)+23) */
	ui &= (uint64_t)-1>>12;
	ui |= (uint64_t)(0x3ff + 23)<<52;
	z = asdbl(ui);
	for (i=0; i < 2; i++) {
		tx[i] = (double)(int32_t)z;
		z     = (z-tx[i])*0x1p24;
	}
	tx[i] = z;
	/* skip zero terms, first term is non-zero */
	while (tx[i] == 0.0)
		i--;
	n = rem_pio2_large(tx,ty,(int)(ix>>20)-(0x3ff+23),i+1,1);
	if (sign) {
		y[0] = -ty[0];
		y[1] = -ty[1];
		return -n;
	}
	y[0] = ty[0];
	y[1] = ty[1];
	return n;
}

/* ------------------------------------------------------------ sin cos tan */

double refmath_sin(double x)
{
	double y[2];
	uint32_t ix = hiword(x) & 0x7fffffff;
	unsigned n;

	if (ix <= 0x3fe921fb) {         /* |x| ~< pi/4 */
		if (ix < 0x3e500000)    /* |x| < 2**-26 */
			return x;
		return k_sin(x, 0.0, 0);
	}
	if (ix >= 0x7ff00000)           /* sin(Inf or NaN) is NaN */
		return x - x;
	n = (unsigned)rem_pio2(x, y);
	switch (n&3) {
	case 0: return  k_sin(y[0], y[1], 1);
	case 1: return  k_cos(y[0], y[1]);
	case 2: return -k_sin(y[0], y[1], 1);
	default:
		return -k_cos(y[0], y[1]);
	}
}

double refmath_cos(double x)
{
	double y[2];
	uint32_t ix = hiword(x) & 0x7fffffff;
	unsigned n;

	if (ix <= 0x3fe921fb) {         /* |x| ~< pi/4 */
		if (ix < 0x3e46a09e)    /* |x| < 2**-27 * sqrt(2) */
			return 1.0;
		return k_cos(x, 0);
	}
	if (ix >= 0x7ff00000)           /* cos(Inf or NaN) is NaN */
		return x - x;
	n = (unsigned)rem_pio2(x, y);
	switch (n&3) {
	case 0: return  k_cos(y[0], y[1]);
	case 1: return -k_sin(y[0], y[1], 1);
	case 2: return -k_cos(y[0], y[1]);
	default:
		return  k_sin(y[0], y[1], 1);
	}
}

double refmath_tan(double x)
{
	double y[2];
	uint32_t ix = hiword(x) & 0x7fffffff;
	unsigned n;

	if (ix <= 0x3fe921fb) {         /* |x| ~< pi/4 */
		if (ix < 0x3e400000)    /* |x| < 2**-27 */
			return x;
		return k_tan(x, 0.0, 0);
	}
	if (ix >= 0x7ff00000)           /* tan(Inf or NaN) is NaN */
		return x - x;
	n = (unsigned)rem_pio2(x, y);
	return k_tan(y[0], y[1], n&1);
}

/* ------------------------------------------------------------ asin acos */

static const double
pio2_hi = 1.57079632679489655800e+00, /* 0x3FF921FB, 0x54442D18 */
pio2_lo = 6.12323399573676603587e-17, /* 0x3C91A626, 0x33145C07 */
pS0 =  1.66666666666666657415e-01, /* 0x3FC55555, 0x55555555 */
pS1 = -3.25565818622400915405e-01, /* 0xBFD4D612, 0x03EB6F7D */
pS2 =  2.01212532134862925881e-01, /* 0x3FC9C155, 0x0E884455 */
pS3 = -4.00555345006794114027e-02, /* 0xBFA48228, 0xB5688F3B */
pS4 =  7.91534994289814532176e-04, /* 0x3F49EFE0, 0x7501B288 */
pS5 =  3.47933107596021167570e-05, /* 0x3F023DE1, 0x0DFDF709 */
qS1 = -2.40339491173441421878e+00, /* 0xC0033A27, 0x1C8A2D4B */
qS2 =  2.02094576023350569471e+00, /* 0x40002AE5, 0x9C598AC8 */
qS3 = -6.88283971605453293030e-01, /* 0xBFE6066C, 0x1B8D0159 */
qS4 =  7.70381505559019352791e-02; /* 0x3FB3B8C5, 0xB12E9282 */

static double asin_r(double z)
{
	double p, q;
	p = z*(pS0+z*(pS1+z*(pS2+z*(pS3+z*(pS4+z*pS5)))));
	q = 1.0+z*(qS1+z*(qS2+z*(qS3+z*qS4)));
	return p/q;
}

double refmath_asin(double x)
{
	double z, r, s, f, c, t;
	uint32_t hx = hiword(x), ix = hx & 0x7fffffff;

	if (ix >= 0x3ff00000) {                 /* |x| >= 1 or nan */
		if (((ix - 0x3ff00000) | loword(x)) == 0)
			return x*pio2_hi + 0x1p-120; /* asin(1) = +-pi/2 */
		return 0/(x-x);
	}
	if (ix < 0x3fe00000) {                  /* |x| < 0.5 */
		/* if 0x1p-1022 <= |x| < 0x1p-26, avoid raising underflow */
		if (ix < 0x3e500000 && ix >= 0x00100000)
			return x;
		return x + x*asin_r(x*x);
	}
	/* 1 > |x| >= 0.5 */
	z = (1 - rm_fabs(x))*0.5;
	s = refmath_sqrt(z);
	r = asin_r(z);
	if (ix >= 0x3fef3333) {                 /* if |x| > 0.975 */
		t = pio2_hi-(2*(s+s*r)-pio2_lo);
	} else {
		/* f+c = sqrt(z) */
		f = lo0(s);
		c = (z-f*f)/(s+f);
		t = 0.5*pio2_hi - (2*s*r - (pio2_lo-2*c) - (0.5*pio2_hi-2*f));
	}
	return (hx >> 31) ? -t : t;
}

double refmath_acos(double x)
{
	double z, w, s, c, df;
	uint32_t hx = hiword(x), ix = hx & 0x7fffffff;

	if (ix >= 0x3ff00000) {                 /* |x| >= 1 or nan */
		if (((ix - 0x3ff00000) | loword(x)) == 0) {
			if (hx >> 31)
				return 2*pio2_hi + 0x1p-120;
			return 0;
		}
		return 0/(x-x);
	}
	if (ix < 0x3fe00000) {                  /* |x| < 0.5 */
		if (ix <= 0x3c600000)           /* |x| < 2**-57 */
			return pio2_hi + 0x1p-120;
		return pio2_hi - (x - (pio2_lo-x*asin_r(x*x)));
	}
	if (hx >> 31) {                         /* x < -0.5 */
		z = (1.0+x)*0.5;
		s = refmath_sqrt(z);
		w = asin_r(z)*s-pio2_lo;
		return 2*(pio2_hi - (s+w));
	}
	/* x > 0.5 */
	z = (1.0-x)*0.5;
	s = refmath_sqrt(z);
	df = lo0(s);
	c = (z-df*df)/(s+df);
	w = asin_r(z)*s+c;
	return 2*(df+w);
}

/* ------------------------------------------------------------ atan atan2 */

static const double atanhi[] = {
  4.63647609000806093515e-01, /* atan(0.5)hi 0x3FDDAC67, 0x0561BB4F */
  7.85398163397448278999e-01, /* atan(1.0)hi 0x3FE921FB, 0x54442D18 */
  9.82793723247329054082e-01, /* atan(1.5)hi 0x3FEF730B, 0xD281F69B */
  1.57079632679489655800e+00, /* atan(inf)hi 0x3FF921FB, 0x54442D18 */
};
static const double atanlo[] = {
  2.26987774529616870924e-17, /* atan(0.5)lo 0x3C7A2B7F, 0x222F65E2 */
  3.06161699786838301793e-17, /* atan(1.0)lo 0x3C81A626, 0x33145C07 */
  1.39033110312309984516e-17, /* atan(1.5)lo 0x3C700788, 0x7AF0CBBD */
  6.12323399573676603587e-17, /* atan(inf)lo 0x3C91A626, 0x33145C07 */
};
static const double aT[] = {
  3.33333333333329318027e-01, /* 0x3FD55555, 0x5555550D */
 -1.99999999998764832476e-01, /* 0xBFC99999, 0x9998EBC4 */
  1.42857142725034663711e-01, /* 0x3FC24924, 0x920083FF */
 -1.11111104054623557880e-01, /* 0xBFBC71C6, 0xFE231671 */
  9.09088713343650656196e-02, /* 0x3FB745CD, 0xC54C206E */
 -7.69187620504482999495e-02, /* 0xBFB3B0F2, 0xAF749A6D */
  6.66107313738753120669e-02, /* 0x3FB10D66, 0xA0D03D51 */
 -5.83357013379057348645e-02, /* 0xBFADDE2D, 0x52DEFD9A */
  4.97687799461593236017e-02, /* 0x3FA97B4B, 0x24760DEB */
 -3.65315727442169155270e-02, /* 0xBFA2B444, 0x2C6A6C2F */
  1.62858201153657823623e-02, /* 0x3F90AD3A, 0xE322DA11 */
};

double refmath_atan(double x)
{
	double w, s1, s2, z;
	uint32_t hx = hiword(x), ix = hx & 0x7fffffff;
	int id;

	if (ix >= 0x44100000) {                 /* if |x| >= 2^66 */
		if (rm_isnan(x))
			return x;
		z = atanhi[3] + 0x1p-120;
		return (hx >> 31) ? -z : z;
	}
	if (ix < 0x3fdc0000) {                  /* |x| < 0.4375 */
		if (ix < 0x3e400000)            /* |x| < 2^-27 */
			return x;
		id = -1;
	} else {
		x = rm_fabs(x);
		if (ix < 0x3ff30000) {          /* |x| < 1.1875 */
			if (ix < 0x3fe60000) {  /*  7/16 <= |x| < 11/16 */
				id = 0;
				x = (2.0*x - 1.0)/(2.0 + x);
			} else {                /* 11/16 <= |x| < 19/16 */
				id = 1;
				x = (x - 1.0)/(x + 1.0);
			}
		} else {
			if (ix < 0x40038000) {  /* |x| < 2.4375 */
				id = 2;
				x = (x - 1.5)/(1.0 + 1.5*x);
			} else {                /* 2.4375 <= |x| < 2^66 */
				id = 3;
				x = -1.0/x;
			}
		}
	}
	/* end of argument reduction */
	z = x*x;
	w = z*z;
	/* break sum from i=0 to 10 aT[i]z**(i+1) into odd and even poly */
	s1 = z*(aT[0]+w*(aT[2]+w*(aT[4]+w*(aT[6]+w*(aT[8]+w*aT[10])))));
	s2 = w*(aT[1]+w*(aT[3]+w*(aT[5]+w*(aT[7]+w*aT[9]))));
	if (id < 0)
		return x - x*(s1+s2);
	z = atanhi[id] - (x*(s1+s2) - atanlo[id] - x);
	return (hx >> 31) ? -z : z;
}

static const double
pi     = 3.1415926535897931160E+00, /* 0x400921FB, 0x54442D18 */
pi_lo  = 1.2246467991473531772E-16; /* 0x3CA1A626, 0x33145C07 */

double refmath_atan2(double y, double x)
{
	double z;
	uint32_t m, lx, ly, ix, iy;

	if (rm_isnan(x) || rm_isnan(y))
		return x+y;
	ix = hiword(x); lx = loword(x);
	iy = hiword(y); ly = loword(y);
	if (((ix-0x3ff00000) | lx) == 0)        /* x = 1.0 */
		return refmath_atan(y);
	m = ((iy>>31)&1) | ((ix>>30)&2);        /* 2*sign(x)+sign(y) */
	ix = ix & 0x7fffffff;
	iy = iy & 0x7fffffff;

	/* when y = 0 */
	if ((iy|ly) == 0) {
		switch(m) {
		case 0:
		case 1: return y;   /* atan(+-0,+anything)=+-0 */
		case 2: return  pi; /* atan(+0,-anything) = pi */
		case 3: return -pi; /* atan(-0,-anything) =-pi */
		}
	}
	/* when x = 0 */
	if ((ix|lx) == 0)
		return m&1 ? -pi/2 : pi/2;
	/* when x is INF */
	if (ix == 0x7ff00000) {
		if (iy == 0x7ff00000) {
			switch(m) {
			case 0: return  pi/4;   /* atan(+INF,+INF) */
			case 1: return -pi/4;   /* atan(-INF,+INF) */
			case 2: return  3*pi/4; /* atan(+INF,-INF) */
			case 3: return -3*pi/4; /* atan(-INF,-INF) */
			}
		} else {
			switch(m) {
			case 0: return  0.0; /* atan(+...,+INF) */
			case 1: return -0.0; /* atan(-...,+INF) */
			case 2: return  pi;  /* atan(+...,-INF) */
			case 3: return -pi;  /* atan(-...,-INF) */
			}
		}
	}
	/* |y/x| > 0x1p64 */
	if (ix+(64<<20) < iy || iy == 0x7ff00000)
		return m&1 ? -pi/2 : pi/2;

	/* z = atan(|y/x|) without spurious underflow */
	if ((m&2) && iy+(64<<20) < ix)  /* |y/x| < 0x1p-64, x<0 */
		z = 0;
	else
		z = refmath_atan(rm_fabs(y/x));
	switch (m) {
	case 0: return z;              /* atan(+,+) */
	case 1: return -z;             /* atan(-,+) */
	case 2: return pi - (z-pi_lo); /* atan(+,-) */
	default: /* case 3 */
		return (z-pi_lo) - pi; /* atan(-,-) */
	}
}

/* ------------------------------------------------------------ exp */

static const double
e_half[2] = {0.5,-0.5},
ln2hi  = 6.93147180369123816490e-01, /* 0x3fe62e42, 0xfee00000 */
ln2lo  = 1.90821492927058770002e-10, /* 0x3dea39ef, 0x35793c76 */
invln2 = 1.44269504088896338700e+00, /* 0x3ff71547, 0x652b82fe */
P1   =  1.66666666666666019037e-01, /* 0x3FC55555, 0x5555553E */
P2   = -2.77777777770155933842e-03, /* 0xBF66C16C, 0x16BEBD93 */
P3   =  6.61375632143793436117e-05, /* 0x3F11566A, 0xAF25DE2C */
P4   = -1.65339022054652515390e-06, /* 0xBEBBBD41, 0xC5D26BF1 */
P5   =  4.13813679705723846039e-08; /* 0x3E663769, 0x72BEA4D0 */

double refmath_exp(double x)
{
	double hi, lo, c, xx, y, dk;
	int k, sign;
	uint32_t hx;

	hx = hiword(x);
	sign = (int)(hx>>31);
	hx &= 0x7fffffff;           /* high word of |x| */

	if (rm_isnan(x))
		return x;
	/* special cases */
	if (hx >= 0x4086232b) {     /* if |x| >= 708.39... */
		if (x > 709.782712893383973096) {
			/* overflow if x!=inf */
			x *= 0x1p1023;
			return x;
		}
		if (x < -708.39641853226410622) {
			/* underflow if x!=-inf */
			if (x < -745.13321910194110842)
				return 0;
		}
	}
	/* argument reduction */
	if (hx > 0x3fd62e42) {      /* if |x| > 0.5 ln2 */
		if (hx > 0x3ff0a2b2)    /* if |x| >= 1.5 ln2 */
			k = (int)(invln2*x + e_half[sign]);
		else
			k = 1 - sign - sign;
		dk = (double)k;
		hi = x - dk*ln2hi;  /* k*ln2hi is exact here */
		lo = dk*ln2lo;
		x = hi - lo;
	} else if (hx > 0x3e300000)  {  /* if |x| > 2**-28 */
		k = 0;
		hi = x;
		lo = 0;
	} else {
		return 1 + x;
	}
	/* x is now in primary range */
	xx = x*x;
	c = x - xx*(P1+xx*(P2+xx*(P3+xx*(P4+xx*P5))));
	y = 1 + (x*c/(2-c) - lo + hi);
	if (k == 0)
		return y;
	return refmath_scalbn(y, k);
}

/* ------------------------------------------------------------ log log10 log2 */

static const double
Lg1 = 6.666666666666735130e-01,  /* 3FE55555 55555593 */
Lg2 = 3.999999999940941908e-01,  /* 3FD99999 9997FA04 */
Lg3 = 2.857142874366239149e-01,  /* 3FD24924 94229359 */
Lg4 = 2.222219843214978396e-01,  /* 3FCC71C5 1D8E78AF */
Lg5 = 1.818357216161805012e-01,  /* 3FC74664 96CB03DE */
Lg6 = 1.531383769920937332e-01,  /* 3FC39A09 D078C69F */
Lg7 = 1.479819860511658591e-01;  /* 3FC2F112 DF3E5244 */

/* Shared reduction: x = 2^k * (1+f), 1+f in [sqrt(2)/2, sqrt(2)].
 * Returns 0 and sets *special when x is 0, negative, inf, nan or 1. */
static int log_reduce(double x, double *fp, int *kp, double *special)
{
	uint64_t ix = asu64(x);
	uint32_t hx = (uint32_t)(ix >> 32);
	int k = 0;

	if (hx < 0x00100000 || hx>>31) {
		if (ix<<1 == 0) {                /* log(+-0)=-inf */
			*special = -1/(x*x);
			return 0;
		}
		if (hx>>31) {                    /* log(-#) = NaN */
			*special = (x-x)/0.0;
			return 0;
		}
		/* subnormal number, scale x up */
		k -= 54;
		x *= 0x1p54;
		ix = asu64(x);
		hx = (uint32_t)(ix >> 32);
	} else if (hx >= 0x7ff00000) {
		*special = x;
		return 0;
	} else if (hx == 0x3ff00000 && ix<<32 == 0) {
		*special = 0;
		return 0;
	}
	/* reduce x into [sqrt(2)/2, sqrt(2)] */
	hx += 0x3ff00000 - 0x3fe6a09e;
	k += (int)(hx>>20) - 0x3ff;
	hx = (hx&0x000fffff) + 0x3fe6a09e;
	ix = (uint64_t)hx<<32 | (ix&0xffffffff);
	*fp = asdbl(ix) - 1.0;
	*kp = k;
	return 1;
}

double refmath_log(double x)
{
	double hfsq, f, s, z, R, w, t1, t2, dk, sp;
	int k;

	if (!log_reduce(x, &f, &k, &sp))
		return sp;
	hfsq = 0.5*f*f;
	s = f/(2.0+f);
	z = s*s;
	w = z*z;
	t1 = w*(Lg2+w*(Lg4+w*Lg6));
	t2 = z*(Lg1+w*(Lg3+w*(Lg5+w*Lg7)));
	R = t2 + t1;
	dk = (double)k;
	return s*(hfsq+R) + dk*ln2lo - hfsq + f + dk*ln2hi;
}

static const double
ivln10hi  = 4.34294481878168880939e-01, /* 0x3fdbcb7b, 0x15200000 */
ivln10lo  = 2.50829467116452752298e-11, /* 0x3dbb9438, 0xca9aadd5 */
log10_2hi = 3.01029995663611771306e-01, /* 0x3FD34413, 0x509F6000 */
log10_2lo = 3.69423907715893078616e-13; /* 0x3D59FEF3, 0x11F12B36 */

double refmath_log10(double x)
{
	double hfsq, f, s, z, R, w, t1, t2, dk, y, hi, lo, val_hi, val_lo, sp;
	int k;

	if (!log_reduce(x, &f, &k, &sp))
		return sp;
	hfsq = 0.5*f*f;
	s = f/(2.0+f);
	z = s*s;
	w = z*z;
	t1 = w*(Lg2+w*(Lg4+w*Lg6));
	t2 = z*(Lg1+w*(Lg3+w*(Lg5+w*Lg7)));
	R = t2 + t1;

	/* hi+lo = f - hfsq + s*(hfsq+R) ~ log(1+f) */
	hi = lo0(f - hfsq);
	lo = f - hi - hfsq + s*(hfsq+R);

	/* val_hi+val_lo ~ log10(1+f) + k*log10(2) */
	val_hi = hi*ivln10hi;
	dk = (double)k;
	y = dk*log10_2hi;
	val_lo = dk*log10_2lo + (lo+hi)*ivln10lo + lo*ivln10hi;

	/* Extra precision in for adding y is not strictly needed
	   since there is no very large cancellation near x = sqrt(2) or
	   x = 1/sqrt(2), but we do it anyway since it costs little on CPUs
	   with some parallelism and it reduces the error for many args. */
	w = y + val_hi;
	val_lo += (y - w) + val_hi;
	val_hi = w;

	return val_lo + val_hi;
}

static const double
ivln2hi = 1.44269504072144627571e+00, /* 0x3ff71547, 0x65200000 */
ivln2lo = 1.67517131648865118353e-10; /* 0x3de705fc, 0x2eefa200 */

double refmath_log2(double x)
{
	double hfsq, f, s, z, R, w, t1, t2, y, hi, lo, val_hi, val_lo, sp;
	int k;

	if (!log_reduce(x, &f, &k, &sp))
		return sp;
	hfsq = 0.5*f*f;
	s = f/(2.0+f);
	z = s*s;
	w = z*z;
	t1 = w*(Lg2+w*(Lg4+w*Lg6));
	t2 = z*(Lg1+w*(Lg3+w*(Lg5+w*Lg7)));
	R = t2 + t1;

	/* hi+lo = f - hfsq + s*(hfsq+R) ~ log(1+f) */
	hi = lo0(f - hfsq);
	lo = f - hi - hfsq + s*(hfsq+R);

	val_hi = hi*ivln2hi;
	val_lo = (lo+hi)*ivln2lo + lo*ivln2hi;

	/* spadd(val_hi, val_lo, y), except for not using double_t: */
	y = (double)k;
	w = y + val_hi;
	val_lo += (y - w) + val_hi;
	val_hi = w;

	return val_lo + val_hi;
}

double refmath_yl2x(double y, double x)
{
	return y * refmath_log2(x);
}

/* ------------------------------------------------------------ x87 stubs */

#if defined(__i386__)

#if defined(_WIN32)
#define RM_SYM(n) "_" #n
#else
#define RM_SYM(n) #n
#endif

/* Frame of every stub, from ESP after the three GPR pushes and pushfl:
 *   [esp+0 .. 8)   first double argument   (or the only one)
 *   [esp+8 .. 16)  second double argument  (binary stubs)
 *   [esp+16]       saved MXCSR
 *   [esp+20]       0x1F80
 * The callee is cdecl, may clobber EAX/ECX/EDX, XMM0-7 and its argument
 * slots, and returns its double in st0. */
#define RM_PRO \
	"	pushfl\n" \
	"	cld\n" \
	"	pushl %eax\n" \
	"	pushl %ecx\n" \
	"	pushl %edx\n" \
	"	subl $24, %esp\n" \
	"	stmxcsr 16(%esp)\n" \
	"	movl $0x1F80, 20(%esp)\n" \
	"	ldmxcsr 20(%esp)\n"
#define RM_EPI \
	"	ldmxcsr 16(%esp)\n" \
	"	addl $24, %esp\n" \
	"	popl %edx\n" \
	"	popl %ecx\n" \
	"	popl %eax\n" \
	"	popfl\n" \
	"	ret\n"
#define RM_GLOBL(name) \
	"	.globl " RM_SYM(name) "\n" \
	"	.p2align 4\n" \
	RM_SYM(name) ":\n"

/* st0 = f(st0) */
#define RM_UNARY(name, fn) \
	RM_GLOBL(name) RM_PRO \
	"	fstpl (%esp)\n" \
	"	call " RM_SYM(fn) "\n" \
	RM_EPI

/* st0 = f(st1, st0), both popped: first argument is st1 */
#define RM_BINARY(name, fn) \
	RM_GLOBL(name) RM_PRO \
	"	fstpl 8(%esp)\n" \
	"	fstpl (%esp)\n" \
	"	call " RM_SYM(fn) "\n" \
	RM_EPI

/* fyl2x with a known st1 constant: pop st0 = x, drop st1, push f(x). */
#define RM_YL2X_CONST(name, fn) \
	RM_GLOBL(name) RM_PRO \
	"	fstpl (%esp)\n" \
	"	fstp %st(0)\n" \
	"	call " RM_SYM(fn) "\n" \
	RM_EPI

__asm__(
	"	.text\n"
	RM_UNARY(refmath_x87_fsin, refmath_sin)
	RM_UNARY(refmath_x87_fcos, refmath_cos)
	/* fptan: st0 = tan(x), then push 1.0 */
	RM_GLOBL(refmath_x87_fptan) RM_PRO
	"	fstpl (%esp)\n"
	"	call " RM_SYM(refmath_tan) "\n"
	"	fld1\n"
	RM_EPI
	RM_BINARY(refmath_x87_fpatan, refmath_atan2)
	RM_YL2X_CONST(refmath_x87_fyl2x_ln, refmath_log)
	RM_YL2X_CONST(refmath_x87_fyl2x_lg, refmath_log10)
	RM_YL2X_CONST(refmath_x87_fyl2x_l2, refmath_log2)
	RM_BINARY(refmath_x87_fyl2x, refmath_yl2x)

	RM_UNARY(refmath_CIsin, refmath_sin)
	RM_UNARY(refmath_CIcos, refmath_cos)
	RM_UNARY(refmath_CItan, refmath_tan)
	RM_UNARY(refmath_CIasin, refmath_asin)
	RM_UNARY(refmath_CIacos, refmath_acos)
	RM_UNARY(refmath_CIatan, refmath_atan)
	RM_BINARY(refmath_CIatan2, refmath_atan2)
	RM_UNARY(refmath_CIexp, refmath_exp)
	RM_UNARY(refmath_CIlog, refmath_log)
	RM_UNARY(refmath_CIlog10, refmath_log10)
	RM_BINARY(refmath_CIpow, refmath_pow)
	RM_UNARY(refmath_CIsqrt, refmath_sqrt)
	RM_BINARY(refmath_CIfmod, refmath_fmod)
);

#endif /* __i386__ */
