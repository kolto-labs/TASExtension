/*
 * golden.c - golden vector for refmath (portable profile, D12).
 *
 * Modes (argv[1]):
 *   ref      refmath cdecl results for a fixed input vector. Built for
 *            x86-windows (run under Wine) and x86_64-linux; the two outputs
 *            must be byte-identical. Its sha256 is the refmath identity.
 *   stubs    (i386) calls every x87 stub through the x87 convention and
 *            checks result bits, x87 stack depth, GPRs, EFLAGS, MXCSR and the
 *            x87 control word.
 *   vanilla  (i386) the same inputs through the raw x87 opcodes the exe uses
 *            (fsin, fcos, fptan, fpatan, fldln2/fldlg2+fyl2x) and the x87
 *            acos/asin sequences rebuilt from 0x6fc118 and 0x6fd028, with the
 *            control word at 0x027F, stored as double.
 *   damage   (i386) all 65535 word inputs through the SetDamage sequence at
 *            0x4d208c..0x4d20ad, vanilla fyl2x against refmath_x87_fyl2x_lg.
 *   consts   (i386) the 80-bit fldln2/fldlg2/fld1 values on this CPU.
 *
 * Doubles are printed as two %08x halves (high word first). Inputs are
 * generated with integer operations only.
 */
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "../refmath.h"

typedef union { double f; uint64_t i; } db;
static uint64_t bits(double x) { db u; u.f = x; return u.i; }
static double dbl(uint64_t i) { db u; u.i = i; return u.f; }
static void phex(double x) { uint64_t u = bits(x); printf(" %08x%08x", (unsigned)(u >> 32), (unsigned)u); }

/* ------------------------------------------------------------ inputs */

static uint64_t rs;
static uint64_t rnd(void) { rs ^= rs << 13; rs ^= rs >> 7; rs ^= rs << 17; return rs; }
static void seed(uint64_t s) { rs = s ? s : 1; }

/* random double, unbiased exponent in [elo, ehi], sign 0 +, 1 -, 2 random */
static double mkd(int sign, int elo, int ehi)
{
	uint64_t r = rnd(), m = rnd() & 0x000fffffffffffffULL;
	int e = elo + (int)(r % (uint64_t)(ehi - elo + 1));
	uint64_t s = sign == 2 ? (r >> 63) : (uint64_t)sign;
	if (e < -1022)
		return dbl(s << 63 | m);            /* subnormal */
	return dbl(s << 63 | (uint64_t)(e + 1023) << 52 | m);
}
static double nudge(double x, int k) { return dbl(bits(x) + (uint64_t)(int64_t)k); }

#define MAXN 4096
static double IN1[MAXN], IN2[MAXN];
static int NIN;
static void add1(double x) { if (NIN < MAXN) IN1[NIN++] = x; }
static void add2(double x, double y) { if (NIN < MAXN) { IN1[NIN] = x; IN2[NIN] = y; NIN++; } }

static const uint64_t SPECIAL[] = {
	0x0000000000000000ULL, 0x8000000000000000ULL, 0x0000000000000001ULL, 0x8000000000000001ULL,
	0x000fffffffffffffULL, 0x0010000000000000ULL, 0x3e10000000000000ULL, 0x3e40000000000000ULL,
	0x3fe0000000000000ULL, 0xbfe0000000000000ULL, 0x3ff0000000000000ULL, 0xbff0000000000000ULL,
	0x3fe921fb54442d18ULL, 0x3ff921fb54442d18ULL, 0x400921fb54442d18ULL, 0x401921fb54442d18ULL,
	0x4012d97c7f3321d2ULL, 0x4059000000000000ULL, 0x412e848000000000ULL, 0x413921fb54442d18ULL,
	0x44b52d02c7e14af6ULL, 0x7e37e43c8800759cULL, 0x7fefffffffffffffULL, 0xffefffffffffffffULL,
	0x7ff0000000000000ULL, 0xfff0000000000000ULL, 0x7ff8000000000000ULL, 0x4000000000000000ULL,
	0xc000000000000000ULL, 0x3fefffffffffffffULL, 0x3ff0000000000001ULL, 0x40862e42fefa39efULL,
	0xc0874910d52d3051ULL, 0x4024000000000000ULL,
};
#define NSPECIAL (int)(sizeof SPECIAL / sizeof SPECIAL[0])

static void gen(const char *fn)
{
	int i, k, n;
	NIN = 0;
	seed(0x9E3779B97F4A7C15ULL ^ (uint64_t)fn[0] << 8 ^ (uint64_t)fn[1] << 16 ^ (uint64_t)fn[2] << 24 ^ (uint64_t)strlen(fn));
	if (!strcmp(fn, "atan2") || !strcmp(fn, "pow") || !strcmp(fn, "fmod")) {
		static const double sp[] = {0.0, -0.0, 1.0, -1.0, 0.5, -0.5, 2.0, -2.0, 3.0, -3.0,
			1e300, -1e300, 4.9e-324, 1.0/0.0, -1.0/0.0, 0.0/0.0};
		int ns = (int)(sizeof sp / sizeof sp[0]);
		for (i = 0; i < ns; i++)
			for (k = 0; k < ns; k++)
				add2(sp[i], sp[k]);
		if (!strcmp(fn, "atan2")) {
			for (i = 0; i < 700; i++) add2(mkd(2, -20, 20), mkd(2, -20, 20));
			for (i = 0; i < 100; i++) add2(mkd(2, -1074, 1023), mkd(2, -1074, 1023));
			for (i = 0; i < 300; i++) {        /* float vector components */
				float a = (float)mkd(2, -8, 7), b = (float)mkd(2, -8, 7);
				add2((double)a, (double)b);
			}
		} else if (!strcmp(fn, "pow")) {
			for (i = 0; i < 700; i++) add2(mkd(0, -100, 100), mkd(2, -10, 4));
			for (i = 0; i < 150; i++) add2(mkd(0, -1074, 1023), mkd(2, -30, 10));
			for (k = -20; k <= 20; k++)
				for (i = 0; i < 6; i++) add2(mkd(1, -8, 8), (double)k);
			for (i = 0; i < 100; i++) add2(nudge(1.0, (int)(rnd() % 2000) - 1000), mkd(2, 20, 60));
			for (k = 0; k < 256; k += 3) {      /* gamma ramp shape: (k/255)^(1/g) */
				float g = (float)(0.5 + (double)(k % 7) * 0.25);
				add2((double)((float)k / 255.0f), (double)(1.0f / g));
			}
			for (k = 1; k <= 40; k++) add2((double)k, 0.4);  /* CR-like */
		} else {
			for (i = 0; i < 700; i++) add2(mkd(2, -60, 60), mkd(2, -60, 60));
			for (i = 0; i < 100; i++) add2(mkd(2, -1074, 1023), mkd(2, -1074, 1023));
			for (i = 0; i < 100; i++) add2(mkd(2, 0, 30), (double)(float)mkd(0, -3, 4));
		}
		return;
	}
	for (i = 0; i < NSPECIAL; i++) add1(dbl(SPECIAL[i]));
	if (!strcmp(fn, "sin") || !strcmp(fn, "cos") || !strcmp(fn, "tan")) {
		for (i = 0; i < 400; i++) add1(mkd(2, -30, 3));
		for (i = 0; i < 150; i++) add1(mkd(2, 4, 20));
		for (i = 0; i < 60; i++) add1(mkd(2, 21, 1023));
		for (k = -720; k <= 720; k += 3) {      /* float degrees to radians */
			float r = (float)k * 0.017453292f;
			add1((double)r);
		}
		for (n = 1; n <= 40; n++) {             /* near n*pi/2 */
			double x = (double)n * 1.57079632679489655800e+00;
			add1(x); add1(nudge(x, 1)); add1(nudge(x, -1)); add1(nudge(x, 2));
		}
		for (n = 0; n < 40; n++) add1((double)(float)mkd(2, -4, 6));
	} else if (!strcmp(fn, "asin") || !strcmp(fn, "acos")) {
		for (i = 0; i < 600; i++) add1(mkd(2, -60, -1));
		for (i = 0; i < 100; i++) add1(nudge(1.0, -(int)(rnd() % 100000)));
		for (i = 0; i < 100; i++) add1(-nudge(1.0, -(int)(rnd() % 100000)));
		for (k = -1000; k <= 1000; k += 5) add1((double)((float)k / 1000.0f));
	} else if (!strcmp(fn, "atan")) {
		for (i = 0; i < 900; i++) add1(mkd(2, -40, 70));
		for (i = 0; i < 100; i++) add1((double)(float)mkd(2, -8, 8));
	} else if (!strcmp(fn, "exp")) {
		for (i = 0; i < 900; i++) add1(mkd(2, -60, 9));
		for (i = 0; i < 60; i++) add1(nudge(709.782712893383973096, (int)(rnd() % 64) - 32));
		for (i = 0; i < 60; i++) add1(nudge(-745.13321910194110842, (int)(rnd() % 64) - 32));
	} else if (!strcmp(fn, "log") || !strcmp(fn, "log10") || !strcmp(fn, "log2")) {
		for (i = 0; i < 900; i++) add1(mkd(0, -1074, 1023));
		for (i = 0; i < 200; i++) add1(1.0 + mkd(2, -40, -2));
		for (k = 1; k <= 1024; k += 3) add1((double)k);
		for (k = 0; k < 32; k++) add1((double)(1u << k));
		for (k = 1; k < 65536; k += 257) add1((double)k);
	} else if (!strcmp(fn, "sqrt")) {
		for (i = 0; i < 600; i++) add1(mkd(0, -1074, 1023));
	} else if (!strcmp(fn, "floor") || !strcmp(fn, "ceil")) {
		for (i = 0; i < 500; i++) add1(mkd(2, -5, 60));
		for (k = -40; k <= 40; k++) add1((double)k * 0.25);
	}
}

typedef double (*f1)(double);
typedef double (*f2)(double, double);
static const struct { const char *name; f1 u; f2 b; } FN[] = {
	{"sin", refmath_sin, 0}, {"cos", refmath_cos, 0}, {"tan", refmath_tan, 0},
	{"asin", refmath_asin, 0}, {"acos", refmath_acos, 0}, {"atan", refmath_atan, 0},
	{"atan2", 0, refmath_atan2}, {"exp", refmath_exp, 0}, {"log", refmath_log, 0},
	{"log10", refmath_log10, 0}, {"log2", refmath_log2, 0}, {"pow", 0, refmath_pow},
	{"sqrt", refmath_sqrt, 0}, {"fmod", 0, refmath_fmod}, {"floor", refmath_floor, 0},
	{"ceil", refmath_ceil, 0},
};
#define NFN (int)(sizeof FN / sizeof FN[0])

static int mode_ref(void)
{
	int f, i;
	printf("# %s\n", REFMATH_VERSION);
	for (f = 0; f < NFN; f++) {
		gen(FN[f].name);
		for (i = 0; i < NIN; i++) {
			printf("%s", FN[f].name);
			if (FN[f].u) {
				phex(IN1[i]); phex(FN[f].u(IN1[i]));
			} else {
				phex(IN1[i]); phex(IN2[i]); phex(FN[f].b(IN1[i], IN2[i]));
			}
			printf("\n");
		}
	}
	return 0;
}

#if defined(__i386__)
/* ------------------------------------------------------------ i386 only */

/*
 * rm_xcall(stub, in, nin, out, nout, st)
 *   loads in[0] (and in[1] on top when nin == 2) onto the x87 stack, sets
 *   EAX/ECX/EDX to sentinels, sets CF and DF, sets MXCSR to 0xFFC0 (round
 *   toward zero, FTZ, DAZ) and the control word to 0x027F, calls stub, then
 *   records into st[]:
 *     0 flags before  1 flags after  2 eax  3 ecx  4 edx  5 mxcsr after
 *     6 fpu cw after  7 fpu sw before loading  8 fpu sw after popping
 *   and pops nout values into out[0..nout) (st0 first).
 */
__asm__(
	"	.text\n"
	"	.globl _rm_xcall\n"
	"_rm_xcall:\n"
	"	pushl %ebp\n"
	"	movl %esp, %ebp\n"
	"	pushl %ebx\n"
	"	pushl %esi\n"
	"	pushl %edi\n"
	"	subl $8, %esp\n"
	"	movl 28(%ebp), %edi\n"          /* st */
	"	fnclex\n"
	"	fnstsw %ax\n"
	"	movzwl %ax, %eax\n"
	"	movl %eax, 28(%edi)\n"
	"	movl 12(%ebp), %esi\n"          /* in */
	"	fldl (%esi)\n"
	"	cmpl $2, 16(%ebp)\n"
	"	jne 1f\n"
	"	fldl 8(%esi)\n"
	"1:\n"
	"	movw $0x027F, (%esp)\n"
	"	fldcw (%esp)\n"
	"	movl $0xFFC0, 4(%esp)\n"
	"	ldmxcsr 4(%esp)\n"
	"	movl 8(%ebp), %ebx\n"           /* stub */
	"	movl $0xA5A5A5A5, %eax\n"
	"	movl $0x5A5A5A5A, %ecx\n"
	"	movl $0xC3C3C3C3, %edx\n"
	"	std\n"
	"	stc\n"
	"	pushfl\n"
	"	popl 0(%edi)\n"
	"	call *%ebx\n"
	"	pushfl\n"
	"	popl 4(%edi)\n"
	"	cld\n"
	"	movl %eax, 8(%edi)\n"
	"	movl %ecx, 12(%edi)\n"
	"	movl %edx, 16(%edi)\n"
	"	stmxcsr 20(%edi)\n"
	"	fnstcw 24(%edi)\n"
	"	movl $0x1F80, 4(%esp)\n"
	"	ldmxcsr 4(%esp)\n"
	"	movl 20(%ebp), %esi\n"          /* out */
	"	fstpl (%esi)\n"
	"	cmpl $2, 24(%ebp)\n"
	"	jne 2f\n"
	"	fstpl 8(%esi)\n"
	"2:\n"
	"	fnstsw %ax\n"
	"	movzwl %ax, %eax\n"
	"	movl %eax, 32(%edi)\n"
	"	addl $8, %esp\n"
	"	popl %edi\n"
	"	popl %esi\n"
	"	popl %ebx\n"
	"	popl %ebp\n"
	"	ret\n"
);
void rm_xcall(void (*stub)(void), const double *in, int nin, double *out, int nout, uint32_t *st);

/* vanilla opcodes: each takes its inputs from in[], returns the result as a
 * double (fstpl, round to nearest), control word 0x027F. */
#define VANILLA(name, body) \
	"	.globl _" #name "\n" \
	"_" #name ":\n" \
	"	subl $4, %esp\n" \
	"	movw $0x027F, (%esp)\n" \
	"	fldcw (%esp)\n" \
	"	movl 8(%esp), %eax\n" \
	body \
	"	movl 12(%esp), %eax\n" \
	"	fstpl (%eax)\n" \
	"	addl $4, %esp\n" \
	"	ret\n"
__asm__(
	"	.text\n"
	VANILLA(v_fsin, "	fldl (%eax)\n	fsin\n")
	VANILLA(v_fcos, "	fldl (%eax)\n	fcos\n")
	VANILLA(v_fptan, "	fldl (%eax)\n	fptan\n	fstp %st(0)\n")
	VANILLA(v_fpatan, "	fldl (%eax)\n	fldl 8(%eax)\n	fpatan\n")      /* atan2(in0, in1) */
	VANILLA(v_ln, "	fldln2\n	fldl (%eax)\n	fyl2x\n")
	VANILLA(v_lg, "	fldlg2\n	fldl (%eax)\n	fyl2x\n")
	VANILLA(v_l2, "	fld1\n	fldl (%eax)\n	fyl2x\n")
	/* CRT _CIacos x87 path, 0x6fc118..0x6fc126 (|x| < 1) */
	VANILLA(v_acos, "	fldl (%eax)\n	fld1\n	fadd %st(1), %st\n	fld1\n	fsub %st(2), %st\n"
			"	fmulp %st, %st(1)\n	fsqrt\n	fxch %st(1)\n	fpatan\n")
	/* CRT _CIasin x87 path, 0x6fd028..0x6fd034 (|x| < 1) */
	VANILLA(v_asin, "	fldl (%eax)\n	fld1\n	fadd %st(1), %st\n	fld1\n	fsub %st(2), %st\n"
			"	fmulp %st, %st(1)\n	fsqrt\n	fpatan\n")
	/* SetDamage 0x4d208c..0x4d20ad with the vanilla fyl2x. in: int32 w */
	"	.globl _v_damage\n"
	"_v_damage:\n"
	"	subl $8, %esp\n"
	"	movw $0x027F, (%esp)\n"
	"	fldcw (%esp)\n"
	"	movl 12(%esp), %eax\n"
	"	fldlg2\n"
	"	fildl (%eax)\n"
	"	fyl2x\n"
	"	fmuls _damage_k\n"
	"	fstps 4(%esp)\n"
	"	flds 4(%esp)\n"
	"	frndint\n"
	"	fistpl 4(%esp)\n"
	"	movl 4(%esp), %eax\n"
	"	addl $8, %esp\n"
	"	ret\n"
	/* the same with the fyl2x replaced by the refmath stub */
	"	.globl _r_damage\n"
	"_r_damage:\n"
	"	subl $8, %esp\n"
	"	movw $0x027F, (%esp)\n"
	"	fldcw (%esp)\n"
	"	movl 12(%esp), %eax\n"
	"	fldlg2\n"
	"	fildl (%eax)\n"
	"	call _refmath_x87_fyl2x_lg\n"
	"	fmuls _damage_k\n"
	"	fstps 4(%esp)\n"
	"	flds 4(%esp)\n"
	"	frndint\n"
	"	fistpl 4(%esp)\n"
	"	movl 4(%esp), %eax\n"
	"	addl $8, %esp\n"
	"	ret\n"
	/* 80-bit constants */
	"	.globl _v_consts\n"
	"_v_consts:\n"
	"	movl 4(%esp), %eax\n"
	"	fldln2\n	fstpt (%eax)\n"
	"	fldlg2\n	fstpt 10(%eax)\n"
	"	fld1\n	fstpt 20(%eax)\n"
	"	ret\n"
);
void v_fsin(const double *, double *);
void v_fcos(const double *, double *);
void v_fptan(const double *, double *);
void v_fpatan(const double *, double *);
void v_ln(const double *, double *);
void v_lg(const double *, double *);
void v_l2(const double *, double *);
void v_acos(const double *, double *);
void v_asin(const double *, double *);
int v_damage(const int32_t *);
int r_damage(const int32_t *);
void v_consts(unsigned char *);
/* SetDamage multiplies by the float at 0x745ff8 (bytes 78 9a 54 40). */
__attribute__((used)) const uint32_t damage_k = 0x40549a78u;

static int nfail;
static void check(const char *what, int ok, const double *in, int nin, const uint32_t *st)
{
	if (ok)
		return;
	nfail++;
	if (nfail <= 40) {
		printf("FAIL %s", what);
		phex(in[0]);
		if (nin == 2) phex(in[1]);
		printf(" flags %08x/%08x regs %08x %08x %08x mxcsr %08x cw %04x sw %04x/%04x\n",
		       st[0], st[1], st[2], st[3], st[4], st[5], st[6] & 0xffff, st[7] & 0xffff, st[8] & 0xffff);
	}
}

static int mode_stubs(void)
{
	static const struct { const char *name; const char *gen; void (*stub)(void); int nin, nout; } S[] = {
		{"x87_fsin", "sin", refmath_x87_fsin, 1, 1},
		{"x87_fcos", "cos", refmath_x87_fcos, 1, 1},
		{"x87_fptan", "tan", refmath_x87_fptan, 1, 2},
		{"x87_fpatan", "atan2", refmath_x87_fpatan, 2, 1},
		{"x87_fyl2x_ln", "log", refmath_x87_fyl2x_ln, 2, 1},
		{"x87_fyl2x_lg", "log10", refmath_x87_fyl2x_lg, 2, 1},
		{"x87_fyl2x_l2", "log2", refmath_x87_fyl2x_l2, 2, 1},
		{"x87_fyl2x", "pow", refmath_x87_fyl2x, 2, 1},
		{"CIsin", "sin", refmath_CIsin, 1, 1}, {"CIcos", "cos", refmath_CIcos, 1, 1},
		{"CItan", "tan", refmath_CItan, 1, 1}, {"CIasin", "asin", refmath_CIasin, 1, 1},
		{"CIacos", "acos", refmath_CIacos, 1, 1}, {"CIatan", "atan", refmath_CIatan, 1, 1},
		{"CIatan2", "atan2", refmath_CIatan2, 2, 1}, {"CIexp", "exp", refmath_CIexp, 1, 1},
		{"CIlog", "log", refmath_CIlog, 1, 1}, {"CIlog10", "log10", refmath_CIlog10, 1, 1},
		{"CIpow", "pow", refmath_CIpow, 2, 1}, {"CIsqrt", "sqrt", refmath_CIsqrt, 1, 1},
		{"CIfmod", "fmod", refmath_CIfmod, 2, 1},
	};
	int s, i, total = 0;
	for (s = 0; s < (int)(sizeof S / sizeof S[0]); s++) {
		gen(S[s].gen);
		for (i = 0; i < NIN; i++) {
			double in[2], out[2] = {0, 0}, want, want2 = 1.0;
			uint32_t st[9];
			int ok;
			in[0] = IN1[i];
			in[1] = IN2[i];
			/* fyl2x variants: st1 is the constant (or y), st0 is x */
			if (!strcmp(S[s].name, "x87_fyl2x_ln")) { in[0] = 0.69314718055994530942; in[1] = IN1[i]; }
			if (!strcmp(S[s].name, "x87_fyl2x_lg")) { in[0] = 0.30102999566398119521; in[1] = IN1[i]; }
			if (!strcmp(S[s].name, "x87_fyl2x_l2")) { in[0] = 1.0; in[1] = IN1[i]; }
			if (!strcmp(S[s].name, "x87_fyl2x")) { in[0] = IN2[i]; in[1] = IN1[i]; }
			rm_xcall(S[s].stub, in, S[s].nin, out, S[s].nout, st);
			if (!strcmp(S[s].name, "x87_fsin") || !strcmp(S[s].name, "CIsin")) want = refmath_sin(in[0]);
			else if (!strcmp(S[s].name, "x87_fcos") || !strcmp(S[s].name, "CIcos")) want = refmath_cos(in[0]);
			else if (!strcmp(S[s].name, "x87_fptan")) { want2 = refmath_tan(in[0]); want = 1.0; }
			else if (!strcmp(S[s].name, "CItan")) want = refmath_tan(in[0]);
			else if (!strcmp(S[s].name, "x87_fpatan") || !strcmp(S[s].name, "CIatan2")) want = refmath_atan2(in[0], in[1]);
			else if (!strcmp(S[s].name, "x87_fyl2x_ln")) want = refmath_log(in[1]);
			else if (!strcmp(S[s].name, "x87_fyl2x_lg")) want = refmath_log10(in[1]);
			else if (!strcmp(S[s].name, "x87_fyl2x_l2")) want = refmath_log2(in[1]);
			else if (!strcmp(S[s].name, "x87_fyl2x")) want = refmath_yl2x(in[0], in[1]);
			else if (!strcmp(S[s].name, "CIasin")) want = refmath_asin(in[0]);
			else if (!strcmp(S[s].name, "CIacos")) want = refmath_acos(in[0]);
			else if (!strcmp(S[s].name, "CIatan")) want = refmath_atan(in[0]);
			else if (!strcmp(S[s].name, "CIexp")) want = refmath_exp(in[0]);
			else if (!strcmp(S[s].name, "CIlog")) want = refmath_log(in[0]);
			else if (!strcmp(S[s].name, "CIlog10")) want = refmath_log10(in[0]);
			else if (!strcmp(S[s].name, "CIpow")) want = refmath_pow(in[0], in[1]);
			else if (!strcmp(S[s].name, "CIsqrt")) want = refmath_sqrt(in[0]);
			else want = refmath_fmod(in[0], in[1]);
			ok = bits(out[0]) == bits(want) || (out[0] != out[0] && want != want);
			if (S[s].nout == 2)
				ok = ok && (bits(out[1]) == bits(want2) || (out[1] != out[1] && want2 != want2));
			check(S[s].name, ok, in, S[s].nin, st);
			/* flags: CF (bit 0) and DF (bit 10) set before, all flags kept */
			check("flags", st[0] == st[1] && (st[0] & 0x401) == 0x401, in, S[s].nin, st);
			check("gpr", st[2] == 0xA5A5A5A5u && st[3] == 0x5A5A5A5Au && st[4] == 0xC3C3C3C3u, in, S[s].nin, st);
			check("mxcsr", (st[5] & 0xFFC0) == 0xFFC0, in, S[s].nin, st);
			check("cw", (st[6] & 0xFFFF) == 0x027F, in, S[s].nin, st);
			/* TOP (bits 11-13) back where it started; SF (bit 6) clear */
			check("top", ((st[7] ^ st[8]) & 0x3800) == 0 && (st[8] & 0x40) == 0, in, S[s].nin, st);
			total++;
		}
	}
	printf("stubs calls=%d failures=%d\n", total, nfail);
	return nfail != 0;
}

static int mode_vanilla(void)
{
	static const struct { const char *name; const char *gen; void (*v)(const double *, double *); int nin; } V[] = {
		{"fsin", "sin", v_fsin, 1}, {"fcos", "cos", v_fcos, 1}, {"fptan", "tan", v_fptan, 1},
		{"fpatan", "atan2", v_fpatan, 2}, {"fyl2x_ln", "log", v_ln, 1}, {"fyl2x_lg", "log10", v_lg, 1},
		{"fyl2x_l2", "log2", v_l2, 1}, {"x87acos", "acos", v_acos, 1}, {"x87asin", "asin", v_asin, 1},
	};
	int v, i;
	for (v = 0; v < (int)(sizeof V / sizeof V[0]); v++) {
		gen(V[v].gen);
		for (i = 0; i < NIN; i++) {
			double in[2], out;
			in[0] = IN1[i]; in[1] = IN2[i];
			if ((V[v].name[0] == 'x') && !(IN1[i] > -1.0 && IN1[i] < 1.0))
				continue;               /* the CRT takes another path for |x| >= 1 */
			V[v].v(in, &out);
			printf("%s", V[v].name);
			phex(in[0]);
			if (V[v].nin == 2) phex(in[1]);
			phex(out);
			printf("\n");
		}
	}
	return 0;
}

static int mode_damage(void)
{
	int32_t w;
	int mism = 0, maxidx = -1000;
	for (w = 1; w <= 65535; w++) {
		int a = v_damage(&w), b = r_damage(&w);
		if (a > maxidx) maxidx = a;
		if (a != b) {
			mism++;
			if (mism <= 20) printf("mismatch w=%d vanilla=%d refmath=%d\n", (int)w, a, b);
		}
	}
	printf("damage inputs=65535 mismatches=%d max_index=%d\n", mism, maxidx);
	return mism != 0;
}

static int mode_consts(void)
{
	unsigned char b[30];
	int i, k;
	static const char *nm[3] = {"fldln2", "fldlg2", "fld1"};
	v_consts(b);
	for (k = 0; k < 3; k++) {
		printf("%s ", nm[k]);
		for (i = 9; i >= 0; i--) printf("%02x", b[k * 10 + i]);
		printf("\n");
	}
	return 0;
}
#endif

int main(int argc, char **argv)
{
	const char *m = argc > 1 ? argv[1] : "ref";
	if (!strcmp(m, "ref")) return mode_ref();
#if defined(__i386__)
	if (!strcmp(m, "stubs")) return mode_stubs();
	if (!strcmp(m, "vanilla")) return mode_vanilla();
	if (!strcmp(m, "damage")) return mode_damage();
	if (!strcmp(m, "consts")) return mode_consts();
#endif
	fprintf(stderr, "unknown mode %s\n", m);
	return 2;
}
