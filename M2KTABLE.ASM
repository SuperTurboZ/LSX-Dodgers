; MZ-2000 matrix, EAh bit 0 first. Unassigned keys leave the scan active.
M2K_KEYS:
	DB 0,0,0,0,0,0,0,0
	DB 0,0,'8','9','0','.','+','-'
	DB '0','1','2','3','4','5','6','7'
	DB 9,' ',13,1EH,1FH,1DH,1CH,3
	DB '/','a','b','c','d','e','f','g'
	DB 'h','i','j','k','l','m','n','o'
	DB 'p','q','r','s','t','u','v','w'
	DB 'x','y','z','^',5CH,'-','.',','
	DB '0','1','2','3','4','5','6','7'
	DB '8','9',':',';','-','@','[',0
	DB ']',0,0CH,8,0,0,0,0
M2K_SHIFT_KEYS:
	DB 0,0,0,0,0,0,0,0
	DB 0,0,'8','9','0','.','+','-'
	DB '0','1','2','3','4','5','6','7'
	DB 9,' ',13,1EH,1FH,1DH,1CH,3
	DB '?','A','B','C','D','E','F','G'
	DB 'H','I','J','K','L','M','N','O'
	DB 'P','Q','R','S','T','U','V','W'
	DB 'X','Y','Z','~','|','=','>','<'
	DB '0','!','"','#','$','%','&',27H
	DB '(',')','*','+','=','`','{',0
	DB '}',0,0CH,8,0,0,0,0

M2K_ESC_TABLE:
	DB 'A'
	DW CTRL1E
	DB 'B'
	DW CTRL1F
	DB 'C'
	DW CTRL1C
	DB 'D'
	DW CTRL1D
	DB 'E'
	DW CTRL0C
	DB 'H'
	DW CTRL0B
	DB 'J'
	DW CTRL06
	DB 'K'
	DW CTRL05
	DB 'L'
	DW CTRL0E
	DB 'Y'
	DW M2K_ESCAPE_LOC
	DB '='
	DW M2K_ESCAPE_LOC
	DB 'j'
	DW CTRL0C
M2K_ESC_COUNT EQU ($-M2K_ESC_TABLE)/3
