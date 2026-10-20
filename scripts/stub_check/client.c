#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
/* usage: client.exe <pubkey_b64_file> <out_file> [clients] */
static int rd(HANDLE h, void *d, DWORD n){DWORD t=0,g=0;while(t<n){if(!ReadFile(h,(char*)d+t,n-t,&g,NULL)||!g)return 0;t+=g;}return 1;}
int main(int argc,char**argv){
  FILE*f=fopen(argv[1],"rb");char key[4096]={0};fread(key,1,sizeof key-1,f);fclose(f);
  int clients = argc>3?atoi(argv[3]):1;
  FILE*o=fopen(argv[2],"wb");
  for(int c=0;c<clients;c++){
  HANDLE h=INVALID_HANDLE_VALUE;
  for(int i=0;i<100&&h==INVALID_HANDLE_VALUE;i++){
    h=CreateFileA("\\\\.\\pipe\\15BADE76-0E87-422B-A75B-B2049FE5F5F5\\STOVE_CHAOSZERO",GENERIC_READ|GENERIC_WRITE,0,NULL,OPEN_EXISTING,0,NULL);
    if(h==INVALID_HANDLE_VALUE)Sleep(200);
  }
  if(h==INVALID_HANDLE_VALUE){fprintf(o,"NOCONNECT\n");return 2;}
  char json[5000];int jl=snprintf(json,sizeof json,"{\"value\":{\"publicKey\":\"%s\",\"processId\":1}}",key);
  unsigned char hdr[4];unsigned tot=jl+4;hdr[0]=tot&255;hdr[1]=tot>>8;hdr[2]=1000&255;hdr[3]=1000>>8;
  DWORD w;WriteFile(h,hdr,4,&w,NULL);WriteFile(h,json,jl,&w,NULL);
  for(int k=0;k<2;k++){
    unsigned char hd[4];if(!rd(h,hd,4)){fprintf(o,"SHORT\n");return 3;}
    unsigned t=hd[0]|(hd[1]<<8),id=hd[2]|(hd[3]<<8);
    char*p=malloc(t);rd(h,p,t-4);p[t-4]=0;
    fprintf(o,"%u %s\n",id,p);free(p);
  }
  CloseHandle(h);
  }
  fclose(o);return 0;}
