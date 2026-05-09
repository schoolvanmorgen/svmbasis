// Verberg het consolevenster op Windows
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    school_van_morgen_lib::run()
}
