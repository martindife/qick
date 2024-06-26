create_clock -period 1.000 -name aclk -waveform {0.000 0.500} [get_ports aclk]
set_input_delay -clock [get_clocks aclk] -min -add_delay 0.200 [get_ports en]
set_input_delay -clock [get_clocks aclk] -max -add_delay 0.400 [get_ports en]
set_input_delay -clock [get_clocks aclk] -min -add_delay 0.200 [get_ports {PINC_REG[*]}]
set_input_delay -clock [get_clocks aclk] -max -add_delay 0.400 [get_ports {PINC_REG[*]}]
set_input_delay -clock [get_clocks aclk] -min -add_delay 0.200 [get_ports {POFF_REG[*]}]
set_input_delay -clock [get_clocks aclk] -max -add_delay 0.400 [get_ports {POFF_REG[*]}]
set_output_delay -clock [get_clocks aclk] -min -add_delay 0.000 [get_ports dout_valid]
set_output_delay -clock [get_clocks aclk] -max -add_delay 0.300 [get_ports dout_valid]
set_output_delay -clock [get_clocks aclk] -min -add_delay 0.000 [get_ports {dout[*]}]
set_output_delay -clock [get_clocks aclk] -max -add_delay 0.300 [get_ports {dout[*]}]

set_false_path -to [get_ports *dout*]
