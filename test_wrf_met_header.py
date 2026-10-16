"""Run the actual met_em header parsing with strict Bash and synthetic input."""
import re
import subprocess
import unittest
from pathlib import Path


SOURCE = Path(__file__).parent / "scripts" / "wrf.sh"
VARIABLES = ("met_levels", "met_st_levels", "met_sm_levels", "actual_we", "actual_sn")
DIMENSIONS = {"num_metgrid_levels": 34, "num_st_layers": 4,
              "num_sm_layers": 4, "west_east": 60, "south_north": 60}


def header(overrides=None):
    values = {**DIMENSIONS, **(overrides or {})}
    dimensions = "".join(f"\t{key} = {value} ;\n" for key, value in values.items()
                         if value is not None)
    # The large trailing metadata makes an early-exit producer failure reliable.
    return "netcdf met_em {\ndimensions:\n" + dimensions + (
        '\tfloat synthetic_metadata ; // trailing attributes\n' * 32768) + "}\n"


def run_shell(code, value):
    return subprocess.run(["bash", "--noprofile", "--norc", "-c",
                           "set -euo pipefail; met_header=$(cat); " + code],
                          input=value, text=True, capture_output=True, timeout=10)


class MetHeaderParsingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text()
        cls.statements = []
        for variable in VARIABLES:
            matches = re.findall(r"^\s*(" + variable + r"=\$\(.+\))$",
                                 cls.source, re.MULTILINE)
            if len(matches) != 1:
                raise AssertionError(f"Expected one actual assignment for {variable}")
            cls.statements.append(matches[0])

    def test_old_producer_pipeline_fails_on_large_header(self):
        old = "value=$(printf '%s\\n' \"$met_header\" | awk " \
              "'/num_st_layers[[:space:]]*=/ {gsub(/[^0-9]/,\"\",$0); print; exit}'); " \
              "printf '%s\\n' \"$value\""
        result = run_shell(old, header())
        self.assertEqual(result.returncode, 141)

    def test_all_actual_assignments_handle_large_header(self):
        statements = "; ".join(self.statements)
        output = "printf '%s\\n' " + " ".join(f'"${v}"' for v in VARIABLES)
        result = run_shell(statements + "; " + output, header())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["34", "4", "4", "60", "60"])

    def test_missing_dimensions_stay_empty(self):
        output = "printf '[%s]\\n' " + " ".join(f'"${v}"' for v in VARIABLES)
        result = run_shell("; ".join(self.statements) + "; " + output,
                           header({key: None for key in DIMENSIONS}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["[]"] * 5)

    def test_actual_atmosphere_and_soil_guards_reject_missing_invalid_values(self):
        begin = self.source.index("        met_levels=$(")
        end = self.source.index("        # Validate each domain/time header", begin)
        block = self.source[begin:end]
        prefix = 'log_info() { :; }; log_error() { printf "%s\\n" "$*" >&2; }; '
        for field, value in [("num_metgrid_levels", None), ("num_metgrid_levels", 0),
                             ("num_st_layers", None), ("num_sm_layers", None),
                             ("num_st_layers", 3), ("num_sm_layers", 5)]:
            with self.subTest(field=field, value=value):
                result = run_shell(prefix + block, header({field: value}))
                self.assertEqual(result.returncode, 1)
                self.assertTrue(result.stderr)

    def test_actual_horizontal_guard_rejects_missing_and_mismatched_sizes(self):
        begin = self.source.index("            actual_we=$(")
        end = self.source.index("        done", begin)
        block = self.source[begin:end]
        prefix = 'expected_we=61; expected_sn=61; met_file=synthetic; ' \
                 'log_error() { printf "%s\\n" "$*" >&2; }; '
        for overrides in [{"west_east": None}, {"south_north": None},
                          {"west_east": 59}, {"south_north": 61}]:
            with self.subTest(overrides=overrides):
                result = run_shell(prefix + block, header(overrides))
                self.assertEqual(result.returncode, 1)
                self.assertIn("SIZE MISMATCH", result.stderr)
        self.assertEqual(run_shell(prefix + block, header()).returncode, 0)


if __name__ == "__main__":
    unittest.main()
