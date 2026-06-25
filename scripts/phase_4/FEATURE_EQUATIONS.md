# Phase 4 Feature Equations

This file describes how the dataset builder computes the Phase 4 features and labels.

## Interval Keys

Each CSV row is keyed by:

- `experiment_dir_name`
- `interval_id`

## EnergyStats Features

The interval-local energystats XML files are not cumulative, so the dataset builder first sums each requested counter across all XML files belonging to the interval.

Let:

- `X_f` be the value of counter `X` in one energystats file `f`
- `Cycles_f` be `total_cycles` in file `f`
- `RealInst_f = total_instructions_f - NOP_instructions_f`

For one interval:

- `SumX = Σ_f X_f`
- `TotalCycles = Σ_f Cycles_f`
- `SumRealInstructions = Σ_f RealInst_f`

For each requested counter `X`, the builder emits:

- `sum_X = SumX`
- `per_cycle_X = SumX / TotalCycles`

The requested raw counters are:

- `cdb_alu_accesses`
- `dcache_read_accesses`
- `rob_reads`
- `busy_cycles`
- `icache_read_accesses`
- `committed_int_instructions`
- `dtlb_total_accesses`
- `itlb_total_misses`
- `btb_read_accesses`
- `dcache_read_misses`
- `cdb_fpu_accesses`
- `branch_mispredictions`
- `dcache_write_accesses`

Derived instruction features:

- `sum_real_instructions = Σ_f RealInst_f`
- `per_cycle_real_instructions = sum_real_instructions / TotalCycles`
- `real_ipc = sum_real_instructions / TotalCycles`

Per-file real IPC:

- `RealIPC_f = RealInst_f / Cycles_f`

Interval-level IPC burstiness feature:

- `max_real_ipc_over_interval_real_ipc = max_f(RealIPC_f) / real_ipc`

## Duty-Cycle Features

For each duty cycle family `D`, with values `D_f` over the XML files in one interval:

- `avg_D = (Σ_f D_f) / N_files`
- `peak_over_avg_D = max_f(D_f) / avg_D`

The duty-cycle families currently emitted are:

- `alu_duty_cycle`
- `mul_cdb_duty_cycle`
- `lsu_duty_cycle`
- `ifu_duty_cycle`
- `fpu_cdb_duty_cycle`

## BB-Derived Features

The interval-local BB catalog contains:

- the static instruction list of each BB
- `interval_execution_count`

For one BB `b`:

- `f_b = interval_execution_count`
- `N_b = number of instructions in BB b`
- `CP_b = conservative static critical-path length`
- `NormCP_b = CP_b / N_b`
- `ILP_b = N_b / CP_b`
- `DynInst_b = f_b * N_b`

### Conservative Critical Path

The implementation builds a conservative static x86 dependency graph per BB:

1. Each instruction is one node.
2. Edges are added using a conservative resource model over:
   - registers
   - flags
   - memory
3. The longest-path depth of each instruction is computed.
4. `CP_b` is the maximum depth in the BB.

This intentionally favors a stable approximation over exact microarchitectural fidelity.

### Interval Aggregates

The builder emits:

- `mean_cp = (Σ_b DynInst_b * CP_b) / (Σ_b DynInst_b)`
- `mean_normalized_cp = (Σ_b DynInst_b * NormCP_b) / (Σ_b DynInst_b)`
- `mean_ilp = (Σ_b DynInst_b * ILP_b) / (Σ_b DynInst_b)`
- `max_critical_path = max_b(CP_b)`

For the “percentage of instructions in critical paths of 3 or longer” feature, the implementation uses:

- `CPInst_b = CP_b` if `CP_b >= 3`, else `0`
- `pct_instructions_in_cp_ge_3 = (Σ_b f_b * CPInst_b) / (Σ_b DynInst_b)`

## Entropy Features

### BB Entropy

For each BB:

- `p_b = DynInst_b / Σ_k DynInst_k`

Then:

- `bb_entropy = - Σ_b p_b log(p_b)`

### Instruction-Category Entropy

Each instruction is assigned to exactly one of these categories:

- `integer_alu`
- `mul_div`
- `fp`
- `simd`
- `load`
- `store`
- `branch`

Let `DynInst_c` be the dynamic instruction count of category `c` in the interval.

- `p_c = DynInst_c / Σ_k DynInst_k`
- `H_cat = - Σ_c p_c log(p_c)`

Normalized category entropy:

- `instruction_category_entropy = H_cat / log(7)`

## Serializing Feature

Serializing mnemonics are:

- `cpuid`
- `mfence`
- `lfence`
- `sfence`
- `rdtsc`
- `wrmsr`
- `call`
- `ret`

Let `SerialDynInst` be the interval-local dynamic count of instructions whose mnemonic is in that set.

- `pct_serializing_instructions = SerialDynInst / Σ_b DynInst_b`

## Instruction Mix Fractions

The builder emits:

- `fraction_integer_alu_instructions`
- `fraction_mul_div_instructions`
- `fraction_fp_instructions`
- `fraction_simd_instructions`
- `fraction_load_instructions`
- `fraction_store_instructions`
- `fraction_branch_instructions`

And also:

- `fraction_lsu_instructions = (fraction_load_instructions + fraction_store_instructions)`

## Intent vs Execution Mismatch

The current emitted mismatch ratios are:

- `alu_intent_vs_execution_mismatch = avg_alu_duty_cycle / fraction_integer_alu_instructions`
- `lsu_intent_vs_execution_mismatch = avg_lsu_duty_cycle / fraction_lsu_instructions`
- `fpu_intent_vs_execution_mismatch = avg_fpu_cdb_duty_cycle / fraction_fp_instructions`

If both numerator and denominator are zero, the implementation returns `0`.
If the denominator is zero and the numerator is nonzero, the implementation records `NaN`, which is written as an empty CSV field.

## Thermal Labels

From `die_grid.temps`:

- `peak_temperature = max(all numeric values in die_grid.temps)`
- `average_temperature = mean(all numeric values in die_grid.temps)`

From `die_grid.temps.2dmaxima.csv`:

- `max_positive_mltd = max(pos_MLTD)`

For one maxima row with temperature `T` and positive MLTD `M`, the builder computes:

- `sigma(x; x0, y0, s, a) = a / (1 + e^(-s(x - x0))) + y0`
- `sigma_df(T) = sigma(T; 115, 0, 0.2, 2)`
- `sigma_M(M) = sigma(M; 25, -0.25, 0.2, 1.25)`
- `sigma_T(T) = sigma(T; 60, 0.35, 0.05, 0.65)`
- `severity(T, M) = sigma_df(T) + sigma_M(M) * sigma_T(T)`

Then:

- `worst_hotspot_severity = max_rows(severity(temp_xy, pos_MLTD))`
