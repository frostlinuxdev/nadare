#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static HANDLE conn(void){HANDLE h=INVALID_HANDLE_VALUE;for(int i=0;i<100&&h==INVALID_HANDLE_VALUE;i++){h=CreateFileA("\\\\.\\pipe\\15BADE76-0E87-422B-A75B-B2049FE5F5F5\\STOVE_CHAOSZERO",GENERIC_READ|GENERIC_WRITE,0,NULL,OPEN_EXISTING,0,NULL);if(h==INVALID_HANDLE_VALUE)Sleep(100);}return h;}
static void send_raw(const unsigned char*p,DWORD n){HANDLE h=conn();if(h==INVALID_HANDLE_VALUE){puts("CONNECT FAILED");exit(2);}DWORD w;WriteFile(h,p,n,&w,NULL);Sleep(60);CloseHandle(h);Sleep(60);}
static void frame(unsigned id,const char*json,unsigned declared){unsigned jl=strlen(json);unsigned char*b=malloc(jl+4);unsigned t=declared?declared:jl+4;b[0]=t&255;b[1]=t>>8;b[2]=id&255;b[3]=id>>8;memcpy(b+4,json,jl);send_raw(b,jl+4);free(b);}
int main(void){
  unsigned char h3[4]={3,0,0,0}; send_raw(h3,4);                     /* total < 4 */
  unsigned char h0[4]={0,0,0,0}; send_raw(h0,4);
  unsigned char hf[4]={0xFF,0xFF,0xE8,0x03}; send_raw(hf,4);          /* claims 65535, sends nothing */
  unsigned char hp[10]={0xFF,0xFF,0xE8,0x03,'a','b','c','d','e','f'}; send_raw(hp,10); /* truncated body */
  frame(1000,"{}",0);
  frame(1000,"{\"value\":{\"publicKey\":\"\"}}",0);
  frame(1000,"{\"value\":{\"publicKey\":\"!!!not base64!!!\"}}",0);
  frame(1000,"{\"value\":{\"publicKey\":\"aGVsbG8=\"}}",0);           /* valid b64, not DER */
  frame(1000,"{\"value\":{\"publicKey\":\"MIIB",0);                   /* unterminated string */
  { char*big=malloc(65000);memset(big,'A',64999);big[64999]=0; char*j=malloc(65100);snprintf(j,65100,"{\"publicKey\":\"%s\"}",big);frame(1000,j,0);}
  frame(9999,"{\"x\":1}",0);
  frame(1001,"{}",0);
  { unsigned char z[8]={8,0,0xE8,3,0,0,0,0}; send_raw(z,8);}         /* embedded NULs */
  puts("FUZZ SENT");return 0;}
