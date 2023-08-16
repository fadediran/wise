 -*- coding: utf-8 -*-
"""Tracé de polygones étoilés
    avec le module Tkinter
"""
 
import Tkinter #le module graphique
import math #module mathématique
 
"""paramètres du graphique"""
Width=300 #largeur de la fenêtre en pixels
Height=300 #hauteur de la fenêtre en pixels
Unit=15 #taille en pixels du vecteur unité
Midx=Width/2 #abscisse du centre du graphique
Midy=Height/2 #ordonnée du centre du graphique
racine=Tkinter.Tk() # la fenêtre de l'application
racine.title("Exemple de représentations graphiques")
fond=Tkinter.Canvas(racine, width=Width, height=Height, background='black')#aire de dessin
 
def convert(x,y):
    """calcule les coordonnées en pixels sur l'image du point de coordonnées x, y réels"""
    m=int (x*Unit)+Midx
    n=Midy-int(y*Unit)
    return m,n
 
def packall(): #initialisation des composants
    fond.pack()
 
 
def Polxy(n):
    """Sommets du polygone régulier"""
    return [(9*math.cos(2*k*math.pi/n),9*math.sin(2*k*math.pi/n)) for k in xrange(0,n)]
 
def Star(n):
    """Tracé polygone étoilé"""
    if(n%2):
        StarImpair(n)
    else:
        StarPair(n)
 
 
def StarImpair(n):
    """ cas impair"""
    P=Polxy(n)
    Q=[convert(x,y) for (x,y) in P]
    for i in xrange(0,2*n,2):
        fond.create_line(Q[i%n][0],Q[i%n][1],Q[(i+2)%n][0],Q[(i+2)%n][1],fill="red")    
 
 
def StarPair(n):
    """cas pair"""
    P=Polxy(n)
    Q=[convert(x,y) for (x,y) in P]
    for i in xrange(0,n,2):
        fond.create_line(Q[i][0],Q[i][1],Q[(i+2)%n][0],Q[(i+2)%n][1],fill="white")
    for i in xrange(1,n,2):
        fond.create_line(Q[i][0],Q[i][1],Q[(i+2)%n][0],Q[(i+2)%n][1],fill="white")        
 
def main():
    """fonction principale"""
    packall()#disposition des composants
    Star(5)# wise
