/*
 * refmath.h - deterministic reference math for the portable profile (D12).
 *
 * Every function here uses only IEEE-754 double operations that SSE2
 * performs exactly (add, sub, mul, div, sqrtsd, conversions) plus integer
 * code. There is no x87 arithmetic, no x87 transcendental opcode, no FMA
 * contraction and no lookup of CPU features. The same input bits therefore
 * give the same output bits on every x86 CPU, native or emulated, as long as
 * MXCSR is 0x1F80 (round to nearest, no FTZ, no DAZ).
 *
 * Two call conventions are provided.
 *
 * 1. cdecl C functions, refmath_<name>(double...). They run under the
 *    caller's MXCSR; the shim pins MXCSR to 0x1F80 on game threads.
 *
 * 2. x87-convention stubs for patching the exe. They take their operands on
 *    the x87 stack and leave the result in st0, exactly like the opcode or
 *    MSVC CRT routine they replace. Each stub preserves EAX, ECX, EDX, all
 *    other GPRs, EFLAGS and MXCSR, clears DF while it runs, and computes with
 *    MXCSR = 0x1F80 whatever the caller had. They do not touch the x87
 *    control word. They are i386 only.
 *
 *    Opcode stubs (inline sites in game code, listed in math_sites.toml):
 *      refmath_x87_fsin         st0 = sin(st0)
 *      refmath_x87_fcos         st0 = cos(st0)
 *      refmath_x87_fptan        st0 = tan(st0), then push 1.0 (as fptan)
 *      refmath_x87_fpatan       st1 = atan2(st1, st0), pop (as fpatan)
 *      refmath_x87_fyl2x_ln     st1 = log(st0), pop  (site loaded st1 by fldln2)
 *      refmath_x87_fyl2x_lg     st1 = log10(st0), pop (site loaded st1 by fldlg2)
 *      refmath_x87_fyl2x_l2     st1 = log2(st0), pop  (site loaded st1 by fld1)
 *      refmath_x87_fyl2x        st1 = st1 * log2(st0), pop (constant unknown)
 *    The fyl2x variant is chosen per site from its fyl2x field in
 *    math_sites.toml (ln, lg, l2 or generic), never by inspecting st1 at
 *    run time: an x87 emulator may hold the fldln2 and fldlg2 constants
 *    with fewer bits, and a run-time match would then fall through to a
 *    different path on that host only.
 *
 *    MSVC CRT intrinsic stubs (_CIxxx convention: arguments on the x87
 *    stack, first argument deepest, result in st0, arguments popped):
 *      refmath_CIsin, _CIcos, _CItan, _CIasin, _CIacos, _CIatan, _CIexp,
 *      _CIlog, _CIlog10, _CIsqrt      unary: st0 -> st0
 *      refmath_CIpow(st1 = x, st0 = y)    pow(x, y)
 *      refmath_CIatan2(st1 = y, st0 = x)  atan2(y, x)
 *      refmath_CIfmod(st1 = x, st0 = y)   fmod(x, y)
 *    K1 GOG 1.03 contains only _CIacos (0x6fc0e0), _CIasin (0x6fcff0) and
 *    _CIpow (0x6fc1f0); _CIpow's operand order was read at 0x423dc0. The other
 *    _CI stubs follow the same documented convention but cannot be checked
 *    against this exe.
 */
#ifndef KPTAS_REFMATH_H
#define KPTAS_REFMATH_H

#ifdef __cplusplus
extern "C" {
#endif

/* Identity of the algorithm set. Bump when any result bit can change, and
 * record the new golden vector digest in golden/digests.txt; golden/check.py
 * fails while the digest for this version is missing or different. */
#define REFMATH_VERSION "refmath/1"

double refmath_sin(double x);
double refmath_cos(double x);
double refmath_tan(double x);
double refmath_asin(double x);
double refmath_acos(double x);
double refmath_atan(double x);
double refmath_atan2(double y, double x);
double refmath_exp(double x);
double refmath_log(double x);
double refmath_log10(double x);
double refmath_log2(double x);
double refmath_pow(double x, double y);
double refmath_sqrt(double x);
double refmath_fmod(double x, double y);
double refmath_floor(double x);
double refmath_ceil(double x);
double refmath_scalbn(double x, int n);
/* y * log2(x), the generic fyl2x value */
double refmath_yl2x(double y, double x);

#if defined(__i386__)
void refmath_x87_fsin(void);
void refmath_x87_fcos(void);
void refmath_x87_fptan(void);
void refmath_x87_fpatan(void);
void refmath_x87_fyl2x_ln(void);
void refmath_x87_fyl2x_lg(void);
void refmath_x87_fyl2x_l2(void);
void refmath_x87_fyl2x(void);

void refmath_CIsin(void);
void refmath_CIcos(void);
void refmath_CItan(void);
void refmath_CIasin(void);
void refmath_CIacos(void);
void refmath_CIatan(void);
void refmath_CIatan2(void);
void refmath_CIexp(void);
void refmath_CIlog(void);
void refmath_CIlog10(void);
void refmath_CIpow(void);
void refmath_CIsqrt(void);
void refmath_CIfmod(void);
#endif

#ifdef __cplusplus
}
#endif
#endif
